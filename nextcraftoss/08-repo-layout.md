# 08 — Repository Layout

Monorepo with two installable packages, lockstep versioned, Apache 2.0.

## Top-level

```
nexcraft/                                  (GitHub: nexcraft-ai/nexcraft)
├── packages/
│   ├── nexcraft/                          # the core library
│   └── nexcraft-jobs/                     # the recipe runtime
├── examples/
├── docs/                                  # mkdocs source
├── benchmarks/
├── .github/workflows/
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── LICENSE                                # Apache 2.0
├── SECURITY.md
└── README.md
```

## `packages/nexcraft/`

```
packages/nexcraft/
├── nexcraft/
│   ├── __init__.py                        # exports public API; nothing else
│   ├── core/
│   │   ├── __init__.py
│   │   ├── protocols.py                   # SourceExecutor, Catalog, ConnectionProvider
│   │   ├── context.py                     # QueryContext, ConnectionHandle
│   │   ├── descriptors.py                 # SourceDescriptor
│   │   └── kinds.py                       # reserved source kinds
│   ├── errors.py                          # NexcraftError + hierarchy
│   ├── catalog/
│   │   ├── __init__.py
│   │   ├── inmemory.py
│   │   └── yaml.py
│   ├── connection/
│   │   ├── __init__.py
│   │   ├── env_vars.py
│   │   └── static.py
│   ├── streaming/
│   │   ├── __init__.py
│   │   ├── cancellable_stream.py
│   │   └── merge.py                       # multi-stream merging for partitioned reads
│   ├── executors/
│   │   ├── __init__.py
│   │   ├── _common.py                     # shared utilities (NEVER imported by core)
│   │   ├── postgres.py                    # extras: [postgres]
│   │   ├── snowflake.py                   # extras: [snowflake]
│   │   ├── bigquery.py                    # extras: [bigquery] — v0.2
│   │   ├── iceberg.py                     # extras: [iceberg]
│   │   └── delta.py                       # extras: [delta]   — v0.2
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── tracing.py                     # OTel
│   │   ├── metrics.py                     # OTel + Prometheus
│   │   └── logging.py                     # structlog setup
│   ├── router.py
│   ├── client.py                          # FedSQLClient
│   ├── server/
│   │   ├── __init__.py
│   │   ├── flight.py                      # extras: [server]
│   │   ├── http.py                        # extras: [server]
│   │   └── proto/                         # generated from .proto files
│   │       └── nexcraft_flight_v1_pb2.py
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── debug_plan.py
│   │   └── serve.py
│   └── testing/
│       ├── __init__.py
│       └── conformance/                   # the pytest plugin
│           ├── __init__.py
│           ├── plugin.py
│           ├── fixtures.py
│           ├── data/                      # shared conformance dataset
│           └── tests/
├── tests/
│   ├── unit/
│   ├── integration/
│   │   ├── postgres/
│   │   ├── snowflake/
│   │   └── iceberg/
│   └── conftest.py
├── pyproject.toml
└── README.md
```

### `pyproject.toml` (excerpt)

```toml
[project]
name = "nexcraft"
version = "0.1.0"
description = "Federated SQL execution for Python with Arrow streaming."
requires-python = ">=3.11"
license = { text = "Apache-2.0" }
dependencies = [
  "pyarrow >= 17.0",
  "structlog >= 24.0",
]

[project.optional-dependencies]
postgres   = ["adbc-driver-postgresql >= 1.0", "asyncpg >= 0.29"]
snowflake  = ["adbc-driver-snowflake >= 1.0"]
bigquery   = ["adbc-driver-bigquery >= 1.0"]
iceberg    = ["datafusion >= 40.0", "pyiceberg >= 0.8"]
delta      = ["datafusion >= 40.0", "deltalake >= 0.20"]
server     = ["fastapi >= 0.110", "uvicorn[standard] >= 0.30", "protobuf >= 5.0"]
otel       = ["opentelemetry-api >= 1.27", "opentelemetry-sdk >= 1.27",
              "opentelemetry-exporter-otlp >= 1.27"]
prometheus = ["prometheus-client >= 0.20"]
all        = ["nexcraft[postgres,snowflake,bigquery,iceberg,delta,server,otel,prometheus]"]
dev        = ["pytest", "pytest-asyncio", "pytest-cov", "ruff", "pyright"]

[project.scripts]
nexcraft = "nexcraft.cli:main"
```

### Why extras-based optional deps

Most users want one or two sources. Forcing every install to pull in Snowflake's transitive dependency tree (which includes a lot) is the kind of thing that gets you uninstalled in security-conscious shops. Extras keep the core install lean (`pyarrow` + `structlog`) and let users opt into what they need.

## `packages/nexcraft-jobs/`

```
packages/nexcraft-jobs/
├── nexcraft_jobs/
│   ├── __init__.py
│   ├── recipe.py                          # Recipe protocol, ComputeResult, ResultRef
│   ├── context.py                         # JobContext (extends QueryContext concepts)
│   ├── runtime/
│   │   ├── __init__.py
│   │   ├── local.py                       # in-process; for dev/tests
│   │   └── temporal.py                    # Temporal worker adapter
│   ├── compute/
│   │   ├── __init__.py
│   │   ├── duckdb_helpers.py              # connection setup, Arrow registration
│   │   └── udfs/
│   │       ├── __init__.py
│   │       ├── timeseries.py              # detrend, stl_decompose, ema
│   │       ├── changepoints.py            # ruptures-based detection
│   │       ├── anomaly.py                 # isolation forest, z-score helpers
│   │       └── forecast.py                # arima, prophet (optional)
│   ├── store/
│   │   ├── __init__.py
│   │   ├── parquet.py                     # write to object storage
│   │   ├── metadata.py                    # job metadata table (Postgres)
│   │   └── refs.py                        # ResultRef + resolution
│   ├── recipes/                           # built-in reference recipes
│   │   ├── __init__.py
│   │   ├── variance.py
│   │   ├── trend.py
│   │   ├── cohort.py
│   │   ├── anomaly.py
│   │   └── what_if.py
│   ├── api/                               # optional HTTP API for job submission
│   │   └── http.py
│   └── cli/
│       └── ...
├── tests/
├── pyproject.toml
└── README.md
```

### `pyproject.toml` (excerpt)

```toml
[project]
name = "nexcraft-jobs"
version = "0.1.0"
description = "Analytical jobs for nexcraft. Recipes on Temporal + DuckDB."
dependencies = [
  "nexcraft == 0.1.0",
  "duckdb >= 1.0",
  "temporalio >= 1.6",
  "pyarrow >= 17.0",
]

[project.optional-dependencies]
forecast   = ["statsmodels >= 0.14", "prophet >= 1.1"]
changepts  = ["ruptures >= 1.1"]
anomaly    = ["scikit-learn >= 1.4"]
all        = ["nexcraft-jobs[forecast,changepts,anomaly]"]
```

## Lockstep versioning

Both packages release together. Versions match. CI enforces:

- `nexcraft-jobs` always pins `nexcraft == <same-version>` exactly.
- A release candidate workflow tags both packages with the same tag (`v0.1.0`) and uploads both to PyPI.

Downstream users can pin either package; the dependency graph keeps them coherent.

## Documentation site

```
docs/
├── mkdocs.yml
├── index.md
├── quickstart/
│   ├── install.md
│   ├── first-query.md
│   └── jobs.md
├── concepts/
│   ├── architecture.md
│   ├── executors.md
│   ├── streaming.md
│   ├── budgets-cancellation.md
│   └── recipes.md
├── how-to/
│   ├── connect-postgres.md
│   ├── connect-snowflake.md
│   ├── connect-iceberg.md
│   ├── write-custom-executor.md           # the make-or-break tutorial
│   ├── write-custom-recipe.md
│   ├── deploy-flight-server.md
│   └── deploy-temporal-worker.md
├── reference/
│   └── (auto-generated from docstrings)
├── adrs/                                  # mirrored from /decisions
└── benchmarks/                            # link to gh-pages
```

mkdocs-material. Mermaid for diagrams. Auto-generated reference from docstrings via `mkdocstrings`.

## Examples

```
examples/
├── 01_postgres_basic.py                   # connect, run query, print Arrow
├── 02_snowflake_partitions.py             # parallel partition fetch
├── 03_iceberg_pushdown.py                 # demonstrate pushed predicates
├── 04_streaming_to_parquet.py             # stream-to-disk pattern
├── 05_flight_server.py                    # run as a service
├── 06_http_server.py                      # FastAPI version
├── 07_recipe_variance.py                  # nexcraft-jobs recipe end-to-end
├── 08_recipe_trend.py                     # STL decomposition
├── 09_temporal_worker.py                  # Temporal worker setup
└── notebooks/
    ├── 01_quickstart.ipynb
    └── 02_recipe_authoring.ipynb
```

All examples are runnable in CI against the same fixtures the conformance suite uses. If an example breaks, the build breaks. Examples are documentation that doesn't lie.
