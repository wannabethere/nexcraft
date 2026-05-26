# Nexcraft examples

Run from the **repository root** after installing both packages in editable mode:

```bash
source .venv/bin/activate
pip install -e "./packages/nexcraft[dev]" -e "./packages/nexcraft-jobs[dev]"
```

Shared helpers live in [`demo_kit.py`](demo_kit.py) (in-memory “warehouse” + `RevenueByRegionRecipe`).

| Script | What it shows |
|--------|----------------|
| [`01_federated_sql_memory.py`](01_federated_sql_memory.py) | Build `FedSQLClient`; run SQL and print results (pandas display). |
| [`02_recipe_local_runtime.py`](02_recipe_local_runtime.py) | Same data via **`LocalRuntime.submit`** (extract → DuckDB → persist). |
| [`03_temporal_submit_sketch.py`](03_temporal_submit_sketch.py) | Start **`nexcraft_recipe_staged`** when `TEMPORAL_HOST` is set. |
| [`04_postgres_vs_snowflake.py`](04_postgres_vs_snowflake.py) | Cross-source recipe: Postgres-dialect SQL + Snowflake-dialect SQL, joined in DuckDB. |
| [`05_api_postgres_vs_snowflake.py`](05_api_postgres_vs_snowflake.py) | Direct `FedSQLClient` API against Postgres/Snowflake sources: `describe`, `execute`, `execute_to_table`, `execute_to_reader`, plus budget enforcement. |
| [`06_db_backed_pooled_provider.py`](06_db_backed_pooled_provider.py) | DB-backed `ConnectionDetails` + `DBCatalog` + `PooledConnectionProvider` with per-kind YAML pool config and tenant validation. |
| [`07_sqlite_management_db.py`](07_sqlite_management_db.py) | Real SQLite as the dummy management DB: schema, seed rows, `SqliteManagementStore`, then run a Postgres-dialect and a Snowflake-dialect query through one `FedSQLClient`. |
| [`08_postgres_env_fedsql.py`](08_postgres_env_fedsql.py) | Real Azure/managed Postgres via `POSTGRES_*` env (optional `NEXCRAFT_DOTENV_PATH` to a `.env` file). **`nexcraft` only** — no Temporal / `nexcraft-jobs`. |
| [`run_demo_worker.py`](run_demo_worker.py) | Minimal worker: `configure_worker`, registry, bundled activities/workflows. |

### Temporal end-to-end (optional)

1. Start Temporal (see [`docs/SETUP.md`](../docs/SETUP.md)).  
2. **Terminal A:** `python examples/run_demo_worker.py`  
3. **Terminal B:** set `NEXCRAFT_STAGING_ROOT` and run `python examples/03_temporal_submit_sketch.py`  

The worker registers **`RevenueByRegionRecipe`** as `revenue_by_region` / `v1`, matching the sketch payload.
