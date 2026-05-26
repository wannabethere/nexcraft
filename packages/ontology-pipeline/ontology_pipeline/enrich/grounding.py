"""Shared prompt-grounding helpers for enrichment stages.

When a `TabularContextBundle` is attached to the `EnrichmentContext` (built by
the foundry profiling pre-pass — see `ontology_pipeline.profile.TableProfiler`),
enricher prompts can include deterministic per-column stats as grounding.

This module renders that grounding into a compact markdown block. Importing
this module is cheap; the actual foundry render is deferred until called.

Usage in an enricher's `_build_prompt`:

    grounding = format_tabular_grounding(ctx, max_sample_rows=10)
    return f\"\"\"...
    {grounding}
    COLUMNS TO ANNOTATE:
    ...\"\"\"

When no bundle is attached (profiling off / failed), returns an empty string,
so the prompt is identical to the pre-foundry behaviour.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ontology_pipeline.enrich.base import EnrichmentContext

logger = logging.getLogger(__name__)


def format_tabular_grounding(
    ctx: "EnrichmentContext",
    *,
    max_sample_rows: int = 10,
    max_chars: int | None = 6000,
    aggregates_only: bool = False,
) -> str:
    """Render the bundle on `ctx` as a markdown grounding block for prompts.

    Args:
        ctx: enrichment context. Reads `ctx.tabular_bundle`.
        max_sample_rows: cap forwarded to foundry's `render_tabular_context`.
            Default 10 keeps tokens low; raise for richer grounding.
        max_chars: hard upper bound on the returned string. Truncates at
            paragraph boundary and appends a `… (truncated)` marker. None disables.
        aggregates_only: when True, strips per-column `top_frequencies` and the
            whole-row `sample_rows` before rendering. Use this for stages that
            shouldn't see raw values (notably `DataProtectionEnricher`, whose
            job is to classify columns BEFORE any value disclosure to an LLM).

    Returns: rendered markdown wrapped with a leading separator + header so
    it slots cleanly into existing prompt templates, OR empty string when
    no bundle is present.
    """
    bundle = getattr(ctx, "tabular_bundle", None)
    if bundle is None:
        return ""
    try:
        from ontology_foundry.context.table_bundle import render_tabular_context
        render_target = bundle
        if aggregates_only:
            # Don't mutate the caller's bundle — model_copy keeps the original
            # intact for the next enricher.
            render_target = bundle.model_copy(update={
                "sample_rows": [],
                "columns": [
                    c.model_copy(update={"top_frequencies": []})
                    for c in bundle.columns
                ],
            })
        rendered = render_tabular_context(
            render_target, max_sample_rows=max_sample_rows,
        )
    except Exception as exc:  # noqa: BLE001 — foundry import / render failures
        logger.debug("format_tabular_grounding: render failed (%s)", exc)
        return ""
    if not rendered.strip():
        return ""
    if max_chars is not None and len(rendered) > max_chars:
        cut = rendered.rfind("\n\n", 0, max_chars)
        if cut < 1000:
            cut = max_chars
        rendered = rendered[:cut].rstrip() + "\n\n… (grounding truncated)\n"
    header = (
        "TABULAR GROUNDING (foundry profile — aggregates only)"
        if aggregates_only
        else "TABULAR GROUNDING (foundry profile)"
    )
    return (
        f"\n--- {header} ---\n"
        f"{rendered.rstrip()}\n"
        f"--- END {header.split(' (')[0]} ---\n"
    )
