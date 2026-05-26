# Nexcraft deployment layouts

Three supported paths: **standalone** (local Python), **Docker Compose** (Temporal + worker), **Kubernetes** (worker + optional submit Job; bring your own Temporal).

---

## Standalone

1. **Python 3.11+** on the host.
2. From the **repository root**:

   ```bash
   ./deploy/standalone/install.sh
   source .venv/bin/activate
   ```

3. **Temporal** (pick one):

   - [Temporal CLI](https://docs.temporal.io/cli/): `temporal server start-dev` → frontend at `localhost:7233`.
   - Or run only the Temporal stack from Docker: `cd deploy/docker && cp .env.example .env` then `docker compose up -d postgresql temporal temporal-ui` (omit `nexcraft-worker` if you run the worker on the host).

4. **Environment** (see `deploy/env.example`):

   - `TEMPORAL_HOST` — e.g. `localhost:7233`
   - `TEMPORAL_NAMESPACE` — default `default`
   - `TEMPORAL_TASK_QUEUE` — default `nexcraft-recipes`
   - `NEXCRAFT_STAGING_ROOT` — writable directory for staged workflows (e.g. `/tmp/nexcraft-staging`)

5. **Worker and client** (repository root, venv active):

   ```bash
   export TEMPORAL_HOST=localhost:7233 NEXCRAFT_STAGING_ROOT=/tmp/nexcraft-staging
   python examples/run_demo_worker.py
   # other terminal:
   python examples/03_temporal_submit_sketch.py
   ```

6. **Tests**: `pytest packages/nexcraft/tests packages/nexcraft-jobs/tests -q`

**External databases**: configure your `FedSQLClient` / connection provider as in `examples/05_api_postgres_vs_snowflake.py` and `examples/06_db_backed_pooled_provider.py`. No in-repo database is required for federation.

---

## Docker Compose

Stack: **PostgreSQL** (Temporal history + **SQL visibility** in DB `temporal_visibility`) **+ Temporal + Temporal UI + Nexcraft worker**. No Elasticsearch.

From **repository root**:

```bash
cd deploy/docker
cp .env.example .env
# optional: edit .env
docker compose up --build
```

- Temporal gRPC: `localhost:7233`
- Temporal UI: `http://localhost:8080`
- Worker uses `TEMPORAL_HOST=temporal:7233` inside the compose network and a named volume at `/var/lib/nexcraft/staging` (keep `NEXCRAFT_STAGING_ROOT` in `.env` aligned with that path).

**Logs to files** (host): with the stack up, from `deploy/docker` run `./scripts/compose-logs-to-file.sh`. That tails `docker compose logs` for `temporal`, `temporal-ui`, `postgresql`, and `nexcraft-worker` and appends to `logs/stack-<timestamp>.log`. Docker also keeps rotated JSON logs per service (`logging` `max-size` / `max-file` in `docker-compose.yml`).

**Workflow visibility** (what the UI lists) is stored in Postgres, not in flat files. For ad-hoc inspection: `docker compose exec postgresql psql -U temporal -d temporal_visibility -c '\dt'`.

If you previously ran the Elasticsearch-based compose here, reset volumes once: `docker compose down -v` (destroys local Temporal/Postgres data).

**Submit a demo workflow from the host** (Temporal published on 7233):

```bash
export TEMPORAL_HOST=localhost:7233 TEMPORAL_NAMESPACE=default TEMPORAL_TASK_QUEUE=nexcraft-recipes
export NEXCRAFT_STAGING_ROOT=/var/lib/nexcraft/staging
```

The staged workflow writes under `staging_root` **on the worker filesystem**. The compose worker only sees the **Docker volume**, not your host `/tmp`. So either:

- Run the submit **inside** the compose network with the same staging path/volume, or  
- Mount a **bind** volume for `nexcraft-staging` if you need the host to see Parquet files.

**Submit from another container** (same compose file; add a one-off service or):

```bash
cd deploy/docker
docker compose run --rm \
  -e TEMPORAL_HOST=temporal:7233 \
  -e NEXCRAFT_STAGING_ROOT=/var/lib/nexcraft/staging \
  nexcraft-worker python /app/examples/03_temporal_submit_sketch.py
```

(Reuse the worker image; override command. Mount the `nexcraft-staging` volume on this run if you add `-v nexcraft-staging:/var/lib/nexcraft/staging` — the submit script only sends `staging_root` in the payload; the worker must have that path. The one-liner above is correct when the worker container shares the same named volume path.)

**Build worker image only** (e.g. for Kubernetes):

```bash
docker build -f deploy/docker/Dockerfile.worker -t nexcraft-worker:latest .
```

---

## Kubernetes

Manifests under `deploy/k8s/` assume:

1. **Temporal** is already available (recommended: [Temporal Helm charts](https://github.com/temporalio/helm-charts) or Temporal Cloud). Adjust `TEMPORAL_HOST` in `worker-deployment.yaml` and `submit-job.yaml` to your frontend address (e.g. `temporal-frontend.temporal.svc.cluster.local:7233`).

2. **Image** `nexcraft-worker:latest` exists in a registry your cluster can pull, or use `kind load docker-image` / `minikube image load` after a local build:

   ```bash
   docker build -f deploy/docker/Dockerfile.worker -t nexcraft-worker:latest .
   ```

3. **Apply**:

   ```bash
   kubectl apply -k deploy/k8s
   kubectl rollout status deployment/nexcraft-worker -n nexcraft --timeout=120s
   ```

   If the submit Job fails because the worker was not registered yet, delete the Job and re-create it after the Deployment is ready.

4. **Demo submit Job** — `submit-job.yaml` runs once. Before re-running:

   ```bash
   kubectl delete job nexcraft-demo-submit -n nexcraft
   kubectl apply -k deploy/k8s
   ```

5. **Staging PVC** is `ReadWriteOnce` and **one worker replica** by default. Scale-out for `nexcraft_recipe_staged` requires a shared filesystem (RWX PVC or object storage + DuckDB config). On clusters that require an explicit `storageClassName`, add it to the PVC in `worker-deployment.yaml`.

6. **Secrets** for external databases: mount via `envFrom` / `secretKeyRef` on the worker Deployment (not generated here).

**Temporal Cloud**: set `TEMPORAL_HOST` to the Cloud gRPC endpoint and extend the Python `Client.connect(...)` calls in your real worker/submit code to use TLS and API key (see [Temporal Python SDK](https://docs.temporal.io/dev-guide/python)); the bundled examples use plaintext `localhost`.

---

## Layout

| Path | Purpose |
|------|---------|
| `deploy/env.example` | Shell env template for standalone / compose hybrid |
| `deploy/docker/` | Compose stack + `Dockerfile.worker` |
| `deploy/docker/scripts/compose-logs-to-file.sh` | Tail compose logs into timestamped files under `deploy/docker/logs/` |
| `deploy/standalone/install.sh` | Local venv + editable installs |
| `deploy/k8s/` | Kustomize bundle: namespace, worker Deployment + PVC, submit Job |

Further API detail: `docs/SETUP.md`, `docs/API_INTEGRATION.md`, `examples/README.md`.
