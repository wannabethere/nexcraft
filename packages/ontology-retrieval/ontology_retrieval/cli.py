"""CLI for ontology-retrieval.

Subcommands:
  serve                          — start the FastAPI app.
  eval run-once                  — process one batch of pending eval_runs.
  eval run-pending               — process N pending eval_runs and exit.
  eval enqueue                   — create a new pending eval_run for the worker.
  eval status                    — show counts by status.
  eval import-cases              — bulk-load eval_case rows from a YAML file.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import click
import uvicorn

from ontology_retrieval.app import create_app


@click.group()
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False))
def main(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


@main.command("serve")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8088, show_default=True, type=int)
@click.option("--db-url", default=None, help="DB URL; falls back to ONTOLOGY_STORE_URL env var.")
@click.option("--reload/--no-reload", default=False)
def cmd_serve(host: str, port: int, db_url: str | None, reload: bool) -> None:
    """Start the retrieval HTTP service."""
    app = create_app(db_url=db_url)
    uvicorn.run(app, host=host, port=port, reload=reload)


# ───────────────────────────────────────────────────────────────────────────
# eval subcommands
# ───────────────────────────────────────────────────────────────────────────

@main.group("eval")
def eval_group() -> None:
    """Eval suite operations — run, enqueue, status, import."""


@eval_group.command("run-pending")
@click.option("--limit", default=1, show_default=True, type=int,
              help="Max pending runs to process this invocation.")
@click.option("--with-llm-judge/--without-llm-judge", default=False, show_default=True,
              help="Enable LLM-as-judge scorer in addition to historical_comparison.")
def cmd_run_pending(limit: int, with_llm_judge: bool) -> None:
    """Pick up pending eval_run rows and execute them."""
    worker, _, _ = _build_eval_worker(with_llm_judge=with_llm_judge)
    stats = worker.run_pending(limit=limit)
    click.echo(
        f"runs_processed={stats.runs_processed} "
        f"succeeded={stats.runs_succeeded} failed={stats.runs_failed} "
        f"cases_evaluated={stats.cases_evaluated}"
    )


@eval_group.command("run-once")
@click.option("--kind", required=True, help="Retrieval kind id (e.g. asset_search).")
@click.option("--scorers", default="historical_comparison",
              help="Comma-separated scorer names; one of: historical_comparison, llm_judge")
@click.option("--case-ids", default=None,
              help="Comma-separated eval_case ids to restrict the run. Empty = all enabled.")
@click.option("--hardness", default=None,
              help="Comma-separated hardness filter (easy, medium, hard).")
def cmd_run_once(kind: str, scorers: str, case_ids: str | None, hardness: str | None) -> None:
    """Create an eval_run + execute it immediately. Useful for ad-hoc local runs."""
    from ontology_store import Database
    from ontology_store.db import EvalRun

    db = Database.from_env()
    scorer_list = [s.strip() for s in scorers.split(",") if s.strip()]

    case_filter: dict[str, Any] = {}
    if case_ids:
        case_filter["case_ids"] = [s.strip() for s in case_ids.split(",") if s.strip()]
    if hardness:
        case_filter["hardness"] = [s.strip() for s in hardness.split(",") if s.strip()]

    with db.session() as s:
        run = EvalRun(
            status="pending",
            retrieval_kind=kind,
            scorer_names=scorer_list,
            case_filter=case_filter,
            trigger="manual",
        )
        s.add(run)
        s.flush()
        run_id = run.run_id
    click.echo(f"enqueued run_id={run_id}; executing now...")

    with_llm = "llm_judge" in scorer_list
    worker, _, _ = _build_eval_worker(with_llm_judge=with_llm)
    case_count = worker.execute_run(run_id)
    click.echo(f"run {run_id} complete: cases_evaluated={case_count}")


@eval_group.command("enqueue")
@click.option("--kind", required=True)
@click.option("--scorers", default="historical_comparison")
@click.option("--case-ids", default=None)
@click.option("--hardness", default=None)
@click.option("--trigger", default="manual", show_default=True)
def cmd_enqueue(kind: str, scorers: str, case_ids: str | None, hardness: str | None, trigger: str) -> None:
    """Create a pending eval_run row without executing it (cron / worker will pick it up)."""
    from ontology_store import Database
    from ontology_store.db import EvalRun

    db = Database.from_env()
    scorer_list = [s.strip() for s in scorers.split(",") if s.strip()]
    case_filter: dict[str, Any] = {}
    if case_ids:
        case_filter["case_ids"] = [s.strip() for s in case_ids.split(",") if s.strip()]
    if hardness:
        case_filter["hardness"] = [s.strip() for s in hardness.split(",") if s.strip()]

    with db.session() as s:
        run = EvalRun(
            status="pending",
            retrieval_kind=kind,
            scorer_names=scorer_list,
            case_filter=case_filter,
            trigger=trigger,
        )
        s.add(run)
        s.flush()
        click.echo(f"enqueued run_id={run.run_id}")


@eval_group.command("status")
def cmd_status() -> None:
    """Show run counts by status + latest run summary."""
    from ontology_store import Database
    from ontology_store.db import EvalRun
    from sqlalchemy import func, select

    db = Database.from_env()
    with db.session() as s:
        for status in ("pending", "running", "done", "failed"):
            n = s.execute(
                select(func.count()).select_from(EvalRun).where(EvalRun.status == status)
            ).scalar_one()
            click.echo(f"  {status:8s} : {n}")
        last = s.execute(
            select(EvalRun).order_by(EvalRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        if last is not None:
            click.echo(
                f"\nLatest run: id={last.run_id} kind={last.retrieval_kind} "
                f"status={last.status} cases={last.case_count} passed={last.passed_count}"
            )


@eval_group.command("import-cases")
@click.option("--path", "path_str", required=True, type=click.Path(exists=True))
@click.option("--upsert/--no-upsert", default=True, show_default=True,
              help="Update existing case_ids in place; otherwise INSERT-only.")
def cmd_import_cases(path_str: str, upsert: bool) -> None:
    """Bulk-load eval_case rows from a YAML file.

    File schema:
        cases:
          - case_id: q001_training_to_attrition
            question: ...
            intent: causal_reasoning
            expected_anchors: [employee, training_assignment, ...]
            expected_asset_rks: [postgres://.../csod_employee, ...]
            forbidden_asset_rks: [...]
            scope_payload: { org_id: acme-corp, concepts: [...] }
            retrieval_kind_default: asset_search
            hardness: medium
            domain_tags: [Clinical, HR]
            enabled: true
            authored_by: jane.k@acme.com
    """
    import yaml
    from ontology_store import Database
    from ontology_store.db import EvalCase

    db = Database.from_env()
    data = yaml.safe_load(Path(path_str).read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not cases:
        raise click.UsageError(f"No `cases:` section in {path_str}")

    inserted = 0
    updated = 0
    skipped = 0
    with db.session() as s:
        for raw in cases:
            cid = raw.get("case_id")
            if not cid:
                skipped += 1
                continue
            existing = s.get(EvalCase, cid)
            if existing is not None:
                if not upsert:
                    skipped += 1
                    continue
                for field, value in raw.items():
                    if field == "case_id":
                        continue
                    setattr(existing, field, value)
                updated += 1
            else:
                s.add(EvalCase(**raw))
                inserted += 1
    click.echo(f"import done: inserted={inserted} updated={updated} skipped={skipped}")


# ───────────────────────────────────────────────────────────────────────────
# Builder helper
# ───────────────────────────────────────────────────────────────────────────

def _build_eval_worker(*, with_llm_judge: bool = False):
    """Build (worker, pipeline, db) wiring."""
    if not os.environ.get("ONTOLOGY_STORE_URL"):
        raise click.UsageError("ONTOLOGY_STORE_URL env var must be set")

    from ontology_store import Database
    from ontology_retrieval.eval import (
        EvalWorker,
        HistoricalComparisonScorer,
        LLMJudgeScorer,
    )
    from ontology_retrieval.pipeline import build_pipeline_from_config, default_config

    db = Database.from_env()
    pipeline = build_pipeline_from_config(default_config(), database=db)

    scorers: list = [HistoricalComparisonScorer()]
    if with_llm_judge:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise click.UsageError(
                "DEEPSEEK_API_KEY env var must be set for --with-llm-judge (DeepSeek V3 default)"
            )
        scorers.append(LLMJudgeScorer())

    worker = EvalWorker(database=db, pipeline=pipeline, scorers=scorers)
    return worker, pipeline, db


if __name__ == "__main__":
    main()
