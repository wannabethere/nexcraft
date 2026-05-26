# _archive — deprecated artefacts, kept for git history

Nothing in this directory is on the import path of `nexcraft_jobs`. The folder
exists so the source of retired code stays one click away from the new code
that replaced it, without anyone accidentally importing it.

## What's here

- `udfs/` — the legacy DuckDB Python-UDF lane. Used to be at
  `nexcraft_jobs/compute/udfs/`. Registered Arrow-vectorised UDFs onto a DuckDB
  connection (calculate_sma, calculate_ema, ml_funnel_json, etc.). The two
  runtimes (`runtime/local.py`, `runtime/temporal_staged_activities.py`) used
  to call `register_analytical_udfs(con)` before handing control to a recipe.
  Replaced by `dstools` SQL templates + Python tools (see
  `genieml/dstools/dstools_functions.json` for the canonical catalog and
  `nexcraft_jobs/compute/dstools_runner.py` for the dispatch layer).

- `tests/test_archived_udfs.py` — the four pytest cases that exercised the
  UDF lane. The file has `pytestmark = pytest.mark.skip(...)` at the top so
  `pytest tests/` from anywhere never collects them. The fifth test in the
  original `test_local_submit.py` (`test_compute_receives_extracted_inputs`)
  doesn't depend on UDFs and stays in active tests.

- `examples/udfs_fake_postgres_pipeline.py` and
  `examples/udfs_fake_snowflake_build_payload.sql` — runnable demos that
  imported `register_analytical_udfs`. Superseded by
  `examples/dstools_smoke.py` and `examples/snowflake_pricemedic.py`.

## Why archived, not deleted

We want the diff history intact: `git log _archive/udfs/__init__.py` still
shows every change. Deletion would force readers to dig through old commits to
understand what the lane used to do — keeping the files visible costs nothing.

## Re-enabling for a one-off (don't ship)

1. Move `_archive/udfs/` back to `nexcraft_jobs/compute/udfs/`.
2. Re-add the imports + `register_analytical_udfs(con)` calls in
   `runtime/local.py` and `runtime/temporal_staged_activities.py` (look at the
   git blame just before the archive commit).
3. Drop the `pytestmark = pytest.mark.skip(...)` line at the top of
   `_archive/tests/test_archived_udfs.py` and move it under `tests/`.

If you find yourself doing this for new work rather than archaeology, the
right answer is almost certainly to add a `dstools` template or Python tool
instead.
