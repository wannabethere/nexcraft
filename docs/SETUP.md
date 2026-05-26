# Nexcraft setup guide

Packaged **standalone**, **Docker Compose**, and **Kubernetes** layouts live under [`deploy/`](../deploy/README.md).

## Requirements

- **Python 3.11+**
- **Temporal** (optional, for `nexcraft-jobs` workflows) — use [Temporal Cloud](https://temporal.io/cloud) or a local cluster  
- **Writable staging directory** (for `nexcraft_recipe_staged`) — local path or shared filesystem workers can all read  

## 1. Clone and virtualenv

```bash
cd /path/to/nexcraft
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

## 2. Install packages (editable)

**pip**

```bash
pip install -e "./packages/nexcraft[dev]" -e "./packages/nexcraft-jobs[dev]"
```

Extras:

- `nexcraft[postgres]`, `nexcraft[snowflake]`, etc. — see `packages/nexcraft/pyproject.toml`  
- `nexcraft-jobs[forecast]` — registers optional `stl_decompose` when `statsmodels` is present  

**uv** (optional)

```bash
uv sync --all-packages
uv run pytest packages/nexcraft/tests packages/nexcraft-jobs/tests -q
```

If `nexcraft-jobs` fails to resolve `nexcraft` outside uv, install `nexcraft` first or keep `[tool.uv.sources]` in `packages/nexcraft-jobs/pyproject.toml`.

## 3. Verify tests

```bash
pytest packages/nexcraft/tests packages/nexcraft-jobs/tests -q
```

## 4. Temporal (production-style recipes)

### 4.1 Run Temporal locally

Use the official [Temporal CLI dev server](https://docs.temporal.io/cli/) or Docker Compose from Temporal docs. You need:

- A reachable **gRPC frontend** (default often `localhost:7233`)  
- A **namespace** (e.g. `default`)  
- A **task queue** name you choose for Nexcraft workers (e.g. `nexcraft-recipes`)  

### 4.2 Worker process

Your worker must:

1. Call **`nexcraft_jobs.runtime.worker_config.configure_worker(fedsql=..., store=...)`** once before handling activities — this wires the shared **`FedSQLClient`** and **`ResultStore`** used by extract/compute/persist.  
2. Register **`NEXCRAFT_RECIPE_ACTIVITIES`** and **`NEXCRAFT_RECIPE_WORKFLOWS`** from `nexcraft_jobs.runtime.temporal_worker_bundle`.  
3. Register every **`Recipe`** implementation on **`nexcraft_jobs.runtime.registry.GLOBAL_REGISTRY`** (`register(recipe)`), matching `recipe_name` / `recipe_version` in `SubmitJobPayload`.  

Minimal sketch:

```python
from temporalio.client import Client
from temporalio.worker import Worker

from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY
from nexcraft_jobs.runtime.temporal_worker_bundle import (
    NEXCRAFT_RECIPE_ACTIVITIES,
    NEXCRAFT_RECIPE_WORKFLOWS,
)
from nexcraft_jobs.runtime.worker_config import configure_worker

# build FedSQLClient + ResultStore for your environment
configure_worker(fedsql=my_fedsql_client, store=my_store)

GLOBAL_REGISTRY.register(MyRecipe())

worker = Worker(
    await Client.connect("localhost:7233"),
    task_queue="nexcraft-recipes",
    workflows=NEXCRAFT_RECIPE_WORKFLOWS,
    activities=NEXCRAFT_RECIPE_ACTIVITIES,
)
await worker.run()
```

### 4.3 Staged workflow filesystem layout

For workflow **`nexcraft_recipe_staged`**, set **`SubmitJobPayload.staging_root`** to an absolute directory path. Extract writes:

```text
{staging_root}/{tenant_id}/{job_id}/extract/{dataset_name}.parquet
```

Workers running compute must see the **same** paths (shared disk, NFS, or equivalent). For object storage (`s3://` URIs), rely on DuckDB’s `read_parquet` configuration and credentials in your deployment (not automated by this repo).

### 4.4 Inline vs staged workflows

| Workflow type name | Use case |
|--------------------|----------|
| `nexcraft_recipe_inline` | Dev / small jobs — single activity runs full `LocalRuntime.submit`. |
| `nexcraft_recipe_staged` | Production-shaped pipeline — validate → Parquet extract (heartbeats) → DuckDB compute → persist. |

See [API & integration](API_INTEGRATION.md) for payload fields and client usage.

## 5. Troubleshooting

- **`ModuleNotFoundError: numpy`** — install `nexcraft-jobs` with its default deps (numpy is required for bundled UDFs).  
- **DuckDB `create_function` / Arrow UDF errors** — Nexcraft targets DuckDB 1.5+ with SQL type strings and `PythonUDFType.ARROW`; upgrade DuckDB if needed.  
- **`staging_root must be set`** — only applies to **`nexcraft_recipe_staged`**; inline workflow does not require it.  
