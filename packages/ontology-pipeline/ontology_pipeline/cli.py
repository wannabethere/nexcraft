"""CLI entry point — `ontology-pipeline run --config <path>`.

Also supports `--dry-run` to introspect and report the table set without
generating MDL or making LLM calls — useful for verifying connection + filter.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from ontology_pipeline.config import PipelineConfig
from ontology_pipeline.introspect import make_introspector
from ontology_pipeline.pipeline import _filter_tables, run  # noqa: PLC2701 — internal helper reuse

logger = logging.getLogger("ontology_pipeline")


@click.group()
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False))
def main(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


@main.command("run")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the pipeline config YAML.")
@click.option("--output-json", type=click.Path(path_type=Path),
              help="Optional path to write the run result as JSON.")
def cmd_run(config_path: Path, output_json: Path | None) -> None:
    """Execute one full pipeline run against the configured source."""
    config = PipelineConfig.load(config_path)
    summary = run(config)

    click.echo(_format_summary(summary))
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        click.echo(f"Run report written to {output_json}")

    if summary.tables_errored > 0:
        sys.exit(2)


@main.command("dry-run")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the pipeline config YAML.")
def cmd_dry_run(config_path: Path) -> None:
    """Introspect the source and print the table set (with filter applied). No LLM, no writes."""
    config = PipelineConfig.load(config_path)
    introspector = make_introspector(config.source.kind)
    introspection = introspector.introspect(source=config.source)
    filtered = _filter_tables(introspection.tables, config.tables)

    click.echo(f"Source: {config.source.source_id}")
    click.echo(f"  catalog: {introspection.catalog}")
    click.echo(f"  schemas: {','.join(config.source.schemas)}")
    click.echo(f"  tables introspected: {len(introspection.tables)}")
    click.echo(f"  tables after filter:  {len(filtered)}")
    click.echo(f"  filter configured:    {config.tables.is_configured()}")
    click.echo("")
    click.echo("Tables that would be processed:")
    for t in filtered:
        kind = "view" if t.is_view else "table"
        cols = len(t.columns)
        comments = sum(1 for c in t.columns if c.description)
        click.echo(f"  - {t.qualified_name}  [{kind}]  cols={cols} (with native description: {comments})")


def _format_summary(summary) -> str:  # PipelineRunResult — typed in pipeline.run
    out = [
        f"Pipeline run for source: {summary.source_id}",
        f"  started:           {summary.started_at}",
        f"  finished:          {summary.finished_at}",
        f"  wall time:         {summary.wall_time_seconds:.1f}s",
        f"  tables seen:       {summary.tables_seen}",
        f"  tables processed:  {summary.tables_processed}",
        f"  tables unchanged:  {summary.tables_skipped_unchanged}",
        f"  tables errored:    {summary.tables_errored}",
        f"  total LLM calls:   {summary.total_llm_calls}",
        "",
    ]
    by_outcome: dict[str, int] = {}
    for r in summary.per_table:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    out.append("Outcome breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(by_outcome.items())))

    if summary.tables_errored:
        out.append("")
        out.append("Errors:")
        for r in summary.per_table:
            if r.outcome == "error":
                out.append(f"  - {r.qualified_name}: {r.error}")
    return "\n".join(out)


if __name__ == "__main__":
    main()
