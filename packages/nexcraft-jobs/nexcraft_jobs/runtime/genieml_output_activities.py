"""Temporal activities: natural-language summary and Vega-Lite chart from query rows."""
from __future__ import annotations

import os
import threading
from typing import Any

from temporalio import activity

from nexcraft_jobs.runtime.genieml_output_models import GeniemlChartParams, GeniemlNarrateParams

_LLM_CONFIGURED = False
_LLM_LOCK = threading.Lock()


def _ensure_skills_llm() -> bool:
    """Configure the genieml_skills registry with an OpenAI LLM runner.

    The worker runs in its own process/venv (separate from context_preparer),
    so the skills registry has no LLM unless we configure it here. Without this,
    `sql.narrate_result` / `chart.generate_vega` fall back to deterministic
    output even though genieml_skills is installed. Idempotent; no-op if no key.
    """
    global _LLM_CONFIGURED
    if _LLM_CONFIGURED:
        return True
    with _LLM_LOCK:
        if _LLM_CONFIGURED:
            return True
        try:
            from genieml_skills.registry import default_registry
        except ImportError:
            return False
        if getattr(default_registry, "_llm", None) is not None:
            _LLM_CONFIGURED = True
            return True
        api_key = (os.getenv("OPENAI_API_KEY") or os.getenv("EMBEDDING_API_KEY") or "").strip()
        if not api_key:
            return False
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_openai import ChatOpenAI
        except ImportError:
            return False
        model = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
        base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        m = model.lower()
        is_reasoning = any(m.startswith(p) for p in ("o1", "o3", "o4", "gpt-5"))
        kwargs: dict[str, Any] = {"model": model, "api_key": api_key, "base_url": base_url}
        if not is_reasoning:
            # Reasoning models (gpt-5/o-series) reject a non-default temperature.
            kwargs["temperature"] = 0.0
        chat = ChatOpenAI(**kwargs)

        async def _runner(system: str, user: str, output_model: type | None):
            if output_model is None:
                raise ValueError("output_model required")
            structured = chat.with_structured_output(output_model, include_raw=True)
            res = await structured.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
            parsed = res.get("parsed") if isinstance(res, dict) else res
            if parsed is None:
                raise ValueError("structured output parse returned None")
            if not isinstance(parsed, output_model):
                parsed = output_model.model_validate(parsed)
            return parsed.model_dump(), {}

        default_registry.configure_llm(_runner)
        _LLM_CONFIGURED = True
        return True


def _deterministic_summary(params: GeniemlNarrateParams) -> dict[str, Any]:
    rows_line = (
        f"**Rows returned:** {params.row_count}"
        if params.row_count is not None
        else "**Rows returned:** _(from job execution)_"
    )
    body = (
        f"### Answer\n\n"
        f"For **{params.question}**, the job returned result rows.\n\n"
        f"{rows_line}\n\n"
        f"```sql\n{params.sql.strip()}\n```\n"
    )
    return {
        "headline": "Query result",
        "body_markdown": body,
        "narration": "Job summary (deterministic).",
        "follow_up_suggestions": [],
    }


def _deterministic_chart(params: GeniemlChartParams) -> dict[str, Any]:
    sample = params.sample_data
    cols = list(sample[0].keys()) if sample else ["metric_value", "category"]
    x_field, y_field = cols[0], cols[1] if len(cols) > 1 else cols[0]
    return {
        "reasoning": "Bar chart from result columns.",
        "chart_type": "bar",
        "chart_schema": {
            "title": params.question[:80] or "Query result",
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": x_field, "type": "nominal", "title": x_field},
                "y": {"field": y_field, "type": "quantitative", "title": y_field},
            },
        },
        "narration": "Chart from job rows.",
        "sql": params.sql,
    }


async def _run_skill(skill_name: str, inputs: dict[str, Any], *, conversation_id: str, org_id: str) -> dict[str, Any]:
    from genieml_skills.registry import default_registry
    from genieml_skills.types import SkillRunContext

    _ensure_skills_llm()  # configure the registry's LLM so skills run for real

    ctx = SkillRunContext(
        skill_name=skill_name,
        step_id="nexcraft_post_output",
        question=str(inputs.get("question") or ""),
        conversation_id=conversation_id,
        org_id=org_id,
        inputs=inputs,
    )
    result = await default_registry.run_skill(ctx)
    out = result.output if isinstance(result.output, dict) else {}
    if not result.success and not out:
        raise RuntimeError(result.error or f"{skill_name} failed")
    return out


@activity.defn(name="genieml_narrate_result")
async def genieml_narrate_result(params: GeniemlNarrateParams | dict[str, Any]) -> dict[str, Any]:
    payload = (
        params
        if isinstance(params, GeniemlNarrateParams)
        else GeniemlNarrateParams.model_validate(params)
    )
    try:
        return await _run_skill(
            "sql.narrate_result",
            {
                "question": payload.question,
                "sql": payload.sql,
                "job_handle": {"row_count": payload.row_count},
                "result_preview": payload.result_preview,
            },
            conversation_id=payload.conversation_id,
            org_id=payload.org_id,
        )
    except ImportError:
        return _deterministic_summary(payload)
    except Exception:
        activity.logger.warning("genieml_narrate_result fell back to deterministic", exc_info=True)
        return _deterministic_summary(payload)


@activity.defn(name="genieml_chart_vega")
async def genieml_chart_vega(params: GeniemlChartParams | dict[str, Any]) -> dict[str, Any]:
    payload = (
        params
        if isinstance(params, GeniemlChartParams)
        else GeniemlChartParams.model_validate(params)
    )
    try:
        return await _run_skill(
            "chart.generate_vega",
            {
                "question": payload.question,
                "sql": payload.sql,
                "sample_data": payload.sample_data,
                "sample_column_values": payload.sample_column_values,
                "language": payload.language,
            },
            conversation_id=payload.conversation_id,
            org_id=payload.org_id,
        )
    except ImportError:
        return _deterministic_chart(payload)
    except Exception:
        activity.logger.warning("genieml_chart_vega fell back to deterministic", exc_info=True)
        return _deterministic_chart(payload)
