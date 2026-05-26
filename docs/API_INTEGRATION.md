# Nexcraft API & integration

This document describes how to integrate applications with **`nexcraft`** (federated execution) and **`nexcraft-jobs`** (recipes + Temporal). There is **no HTTP REST layer** in-repo; integrate via Python APIs or Temporal clients.

## Examples (runnable)

See the [`examples/`](../examples/) directory:

- **`01_federated_sql_memory.py`** — minimal `FedSQLClient` usage  
- **`02_recipe_local_runtime.py`** — `LocalRuntime` + recipe phases  
- **`03_temporal_submit_sketch.py`** + **`run_demo_worker.py`** — staged Temporal workflow against the demo registry  

Details in [`examples/README.md`](../examples/README.md).

---

## 1. Nexcraft core — `FedSQLClient`

### 1.1 Responsibilities

- You supply **dialect-correct SQL** for **one source** per call (`source_id`).  
- Nexcraft resolves the source via a **`Catalog`**, acquires a **`ConnectionHandle`** through a **`ConnectionProvider`**, and dispatches to a **`SourceExecutor`**.  
- Results stream as **`pyarrow.RecordBatch`** (optional **`CancellableArrowStream`** wrapper for deadlines and budgets).  

### 1.2 Minimal wiring

Conceptually:

```python
from nexcraft import FedSQLClient, QueryContext
from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router

catalog = InMemoryCatalog({...})
provider = StaticConnectionProvider({...})
router = Router(catalog=catalog, connection_provider=provider, executors={"memory": MemoryExecutor()})
client = FedSQLClient(router)

ctx = QueryContext(tenant_id="acme", query_id="01HZ...")
table = await client.execute_to_table("my_source", "SELECT 1", ctx)
```

Public exports from `nexcraft` include **`FedSQLClient`**, **`QueryContext`**, **`SourceDescriptor`**, **`SourceExecutor`**, **`Catalog`**, **`ConnectionProvider`**, **`ConnectionHandle`**, and **`NexcraftError`** subclasses.

### 1.3 Error mapping

Executors should raise **`nexcraft.errors`** types (e.g. **`BudgetExceededError`**, **`SourceSyntaxError`**, **`ConnectionError`**). Temporal activities map selected types to non-retryable **`ApplicationError`** for staged workflows.

---

## 2. Nexcraft Jobs — recipes

### 2.1 `Recipe` protocol

Implement:

| Phase | Method | Notes |
|-------|--------|--------|
| Validate | `validate(params)` | Deterministic; raise **`ValueError`** for bad inputs. |
| Extract | `extract(params, ctx, fedsql)` | Async; return **`Mapping[str, pa.Table \| pa.RecordBatchReader]`** keyed by logical dataset names used in SQL. |
| Compute | `compute(params, ctx, con)` | Async; DuckDB connection already has extract views (staged) or registered Arrow tables (local). |
| Persist | `persist(result, params, ctx, store)` | Async; returns **`ResultRef`**. |

Each recipe exposes **`name`** and **`version`** (Temporal resolves **`GLOBAL_REGISTRY`** by `(name, version)`).

### 2.2 `JobContext`

Carries **`tenant_id`**, **`job_id`**, nested **`QueryContext`** (`query`), and DuckDB tuning: **`memory_budget`**, **`cpu_budget`**, **`scratch_dir`**.

### 2.3 `LocalRuntime` (no Temporal)

```python
from nexcraft_jobs.runtime.local import LocalRuntime

runtime = LocalRuntime(fedsql_client, store=None)  # None → NullResultStore
ref = await runtime.submit(my_recipe, params={}, ctx=job_ctx)
```

Flow: validate → extract (Arrow) → **`setup_duckdb`** → **`register_extract_streams`** → **`register_analytical_udfs`** → compute → persist.

### 2.4 `ResultStore`

Implement **`nexcraft_jobs.recipe.ResultStore`** (`finalize(...) -> ResultRef`). **`NullResultStore`** returns a placeholder URI for development.

---

## 3. Temporal integration

### 3.1 Payload — `SubmitJobPayload`

Defined in **`nexcraft_jobs.runtime.temporal_payloads`**. Fields:

| Field | Purpose |
|-------|---------|
| `recipe_name`, `recipe_version` | Registry lookup |
| `params` | JSON-friendly dict passed to each phase |
| `tenant_id`, `job_id`, `query_id` | Identity; `query_id` feeds **`QueryContext`** |
| `trace_id` | Optional tracing |
| `memory_budget`, `cpu_budget`, `scratch_dir` | DuckDB / spill |
| **`staging_root`** | **Required** for **`nexcraft_recipe_staged`** — filesystem root for Parquet extract |

### 3.2 Workflow type names

Use these strings with **`Client.start_workflow`**:

- **`nexcraft_recipe_inline`** — argument: single **`SubmitJobPayload`**; result: **`ResultRef`**.  
- **`nexcraft_recipe_staged`** — same argument/result; adds Parquet staging and separate activities.  

### 3.3 Activities (registration)

Registered in **`nexcraft_jobs.runtime.temporal_worker_bundle.NEXCRAFT_RECIPE_ACTIVITIES`**:

- `validate_recipe_activity`  
- `run_recipe_inline_activity`  
- `run_extract_to_parquet_activity`  
- `run_compute_from_parquet_activity`  
- `run_persist_activity`  

Workflows invoke several by **string name** (e.g. `"validate_recipe_activity"`).

### 3.4 Worker configuration

Before starting the worker:

```python
from nexcraft_jobs.runtime.worker_config import configure_worker

configure_worker(fedsql=my_fedsql_client, store=my_store)
```

Register recipes:

```python
from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY

GLOBAL_REGISTRY.register(MyRecipe())
```

### 3.5 Client example (Temporal Python SDK)

```python
from temporalio.client import Client

from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload

payload = SubmitJobPayload(
    recipe_name="my_recipe",
    recipe_version="v1",
    params={"window_days": 30},
    tenant_id="acme",
    job_id="01HZXXX",
    query_id="01HZYYY",
    staging_root="/var/nexcraft/staging",
)

client = await Client.connect("localhost:7233", namespace="default")
handle = await client.start_workflow(
    "nexcraft_recipe_staged",
    args=[payload],
    id=f"nexcraft-job-{payload.job_id}",
    task_queue="nexcraft-recipes",
)
result_ref = await handle.result()
```

Adjust **`task_queue`**, **`namespace`**, and **`workflow id`** to your policies.

---

## 4. Extension points

| Concern | Hook |
|---------|------|
| New warehouse / lake source | Implement **`SourceExecutor`**, register on **`Router`**. |
| Secrets / pooling | Custom **`ConnectionProvider`**. |
| Catalog | Implement **`Catalog`** (reference: **`InMemoryCatalog`**). |
| Result durability | Implement **`ResultStore`** (e.g. S3 + Postgres index per design docs). |

---

## 5. Related design docs

- `nextcraftoss/02-protocols.md` — core nexcraft contracts  
- `nextcraftoss/02-temporal.md` — Temporal staging model  
- `nextcraftoss/03-duckdb-udfs.md` — DuckDB UDFs and SQL-first recipes  
