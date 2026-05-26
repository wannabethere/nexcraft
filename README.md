# Nexcraft

Python monorepo for **federated single-source SQL execution** (`nexcraft`) and **analytical recipes** (`nexcraft-jobs`) with optional **Temporal** orchestration and **DuckDB** compute.

| Package | Role |
|---------|------|
| `packages/nexcraft` | Run dialect-correct SQL against one source at a time; stream Arrow results with cancellation and budgets. |
| `packages/nexcraft-jobs` | Four-phase recipes (validate → extract → compute → persist), `LocalRuntime`, and Temporal workflows with Parquet staging. |

## Documentation

- [Deploy layouts](deploy/README.md) — standalone, Docker Compose (Temporal + worker), Kubernetes  
- [Setup guide](docs/SETUP.md) — environments, installs, Temporal worker, staging directories  
- [API & integration](docs/API_INTEGRATION.md) — `FedSQLClient`, recipes, Temporal payloads, worker wiring  
- [Examples](examples/README.md) — runnable scripts (`FedSQLClient`, `LocalRuntime`, Temporal sketch + demo worker)  

Design notes live under [`nextcraftoss/`](nextcraftoss/).

## Quick install

From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e "./packages/nexcraft[dev]" -e "./packages/nexcraft-jobs[dev]"
pytest packages/nexcraft/tests packages/nexcraft-jobs/tests -q
```


Changes
nexcraft-jobs/pyproject.toml:

dependencies = [
    "nexcraft>=0.1.0,<0.2",
    "dstools>=0.1.0",          # added
    ...
]

[tool.uv.sources]
nexcraft = { workspace = true }
dstools  = { path = "../../../genieml/dstools", editable = true }   # added
Verified
uv sync --all-packages from the workspace root resolves dstools as editable at ../genieml/dstools (captured in nexcraft/uv.lock).
uv pip show dstools reports it installed with Required-by: nexcraft-jobs.
python examples/dstools_smoke.py still runs end-to-end; both SQL and Python lanes return the same retention table.
How teammates onboard
cd nexcraft
uv sync --all-packages
That picks up the editable link automatically. As long as genieml/dstools/ lives at the relative path, no extra setup is needed.

Optional **uv** workspace is configured in the root `pyproject.toml`.

## License

Apache-2.0 (see `LICENSE`).
