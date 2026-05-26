"""
Integration: combined ask API → SQL → Postgres (FedSQL, same pattern as ``examples/08_postgres_env_fedsql.py``).

Questions are loaded from ``tests/fixtures/crosscsod_workdayquestions.md``. Writes a CSV per run.
Some rows may be ``failed`` (e.g. no SQL from the combined API); that is expected — the test still
**passes** and you inspect the CSV. Set ``CSOD_WORKDAY_STRICT=1`` to ``pytest.fail`` if any question
did not end in ``success``.

From ``packages/nexcraft``::

    export RUN_CSOD_WORKDAY_INTEGRATION=1
    export COMBINED_ASK_BASE_URL=http://100.26.125.159:8025
    export NEXCRAFT_DOTENV_PATH=/path/to/.env
    export CSOD_WORKDAY_QUESTION_LIMIT=5   # optional: first N only; omit or ``0`` = all questions
    export CSOD_WORKDAY_RESULTS_CSV=tests/output/run.csv
    pytest tests/test_cross_csod_workday_combined_sql.py -m integration -v

Requires ``nexcraft[postgres]`` dev install and ``requests`` (dev extra).
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from cross_csod_postgres_env import load_dotenv_file, run_sql_with_env
from cross_csod_workday_questions import load_questions

PROJECT_ID = "csodworkday"
COMBINED_REL_PATH = "/api/v1/combined/combined"
DEFAULT_COMBINED_BASE = "http://100.26.125.159:8025"


def _questions_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "crosscsod_workdayquestions.md"


def _output_csv_path() -> Path:
    explicit = os.environ.get("CSOD_WORKDAY_RESULTS_CSV")
    if explicit:
        return Path(explicit).expanduser().resolve()
    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return out_dir / f"cross_csod_workday_run_{ts}.csv"


def _limited_questions() -> list[str]:
    """All questions from the fixture by default; set ``CSOD_WORKDAY_QUESTION_LIMIT`` to a positive int for a cap."""
    qs = load_questions(_questions_path())
    raw = (os.environ.get("CSOD_WORKDAY_QUESTION_LIMIT") or "0").strip()
    if raw == "" or raw == "0":
        return qs
    n = max(1, int(raw))
    return qs[:n]


def _integration_allowed() -> bool:
    return os.environ.get("RUN_CSOD_WORKDAY_INTEGRATION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _postgres_env_ready() -> bool:
    for key in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"):
        if not os.environ.get(key):
            return False
    return True


def _load_dotenv_if_configured() -> None:
    """Populate ``os.environ`` from ``NEXCRAFT_DOTENV_PATH`` before readiness checks."""
    path = os.environ.get("NEXCRAFT_DOTENV_PATH")
    if path:
        load_dotenv_file(path)


def _reasoning_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    r = data.get("sql_generation_reasoning")
    if isinstance(r, str) and r.strip():
        parts.append(r.strip())
    elif r is not None:
        parts.append(json.dumps(r, default=str, ensure_ascii=False))
    exp = data.get("explanation")
    if isinstance(exp, str) and exp.strip():
        parts.append(f"explanation: {exp.strip()}")
    return "\n\n".join(parts) if parts else ""


def extract_sql_from_combined_response(data: dict[str, Any]) -> str | None:
    if data.get("invalid_sql"):
        return None
    err = data.get("error")
    if err:
        return None
    for item in data.get("response") or []:
        if isinstance(item, dict):
            sql = item.get("sql")
            if isinstance(sql, str) and sql.strip():
                return sql.strip()
    return None


def post_combined_ask(base_url: str, question: str) -> dict[str, Any]:
    import requests

    url = base_url.rstrip("/") + COMBINED_REL_PATH
    payload: dict[str, Any] = {
        "query": question,
        "project_id": PROJECT_ID,
        "histories": [],
    }
    timeout_s = float(os.environ.get("COMBINED_ASK_TIMEOUT_S", "600"))
    r = requests.post(url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def _write_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "full_response",
        "sql_extracted",
        "outcome",
        "num_rows",
        "reasoning",
        "question",
        "project_id",
        "error_detail",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
        )
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def test_cross_csod_workday_question_catalog_parses() -> None:
    path = _questions_path()
    assert path.is_file(), f"Missing markdown: {path}"
    qs = load_questions(path)
    assert len(qs) >= 5, f"Expected multiple questions, got {len(qs)}"


@pytest.mark.integration
def test_cross_csod_workday_combined_then_postgres() -> None:
    if not _integration_allowed():
        pytest.skip("Set RUN_CSOD_WORKDAY_INTEGRATION=1 to run this test.")

    pytest.importorskip("asyncpg")
    pytest.importorskip("requests")

    _load_dotenv_if_configured()
    if not _postgres_env_ready():
        pytest.skip(
            "Set POSTGRES_HOST, POSTGRES_USER, POSTGRES_DB, POSTGRES_PASSWORD in the environment, "
            "or set NEXCRAFT_DOTENV_PATH to a .env file that defines them (loaded before this check)."
        )

    base = os.environ.get("COMBINED_ASK_BASE_URL", DEFAULT_COMBINED_BASE).rstrip("/")
    questions = _limited_questions()
    failures: list[str] = []
    csv_path = _output_csv_path()
    rows_out: list[dict[str, Any]] = []

    for q in questions:
        row: dict[str, Any] = {
            "full_response": "",
            "sql_extracted": "",
            "outcome": "failed",
            "num_rows": "",
            "reasoning": "",
            "question": q,
            "project_id": PROJECT_ID,
            "error_detail": "",
        }
        try:
            data = post_combined_ask(base, q)
            row["full_response"] = json.dumps(data, default=str, ensure_ascii=False)
            row["reasoning"] = _reasoning_text(data)

            status = data.get("status")
            if status != "finished":
                row["error_detail"] = json.dumps(data.get("error"), default=str) if data.get("error") else f"status={status!r}"
                failures.append(f"[{status!r}] {q[:80]!r}… error={data.get('error')}")
                rows_out.append(row)
                continue

            sql = extract_sql_from_combined_response(data)
            if not sql:
                row["error_detail"] = "no_sql_in_response"
                failures.append(f"[no sql] {q[:80]!r}…")
                rows_out.append(row)
                continue

            row["sql_extracted"] = sql
            table = asyncio.run(run_sql_with_env(sql))
            row["num_rows"] = str(table.num_rows)
            row["outcome"] = "success"
            rows_out.append(row)
        except Exception as exc:
            row["error_detail"] = repr(exc)
            failures.append(f"{q[:80]!r}… → {exc!r}")
            if not row["full_response"]:
                row["full_response"] = json.dumps({"_exception": repr(exc)}, ensure_ascii=False)
            rows_out.append(row)

    _write_results_csv(csv_path, rows_out)
    ok = sum(1 for r in rows_out if r.get("outcome") == "success")
    bad = len(rows_out) - ok
    print(f"\nWrote results CSV: {csv_path} ({len(rows_out)} rows, {ok} success, {bad} failed)\n")
    if failures:
        print("Failures (expected for some questions; see CSV error_detail / outcome):")
        for line in failures[:30]:
            print(" ", line)
        if len(failures) > 30:
            print(f" … and {len(failures) - 30} more")

    if os.environ.get("CSOD_WORKDAY_STRICT", "").strip().lower() in ("1", "true", "yes", "on"):
        assert not failures, ";\n".join(failures[:30]) + (
            f"\n… and {len(failures) - 30} more" if len(failures) > 30 else ""
        )
