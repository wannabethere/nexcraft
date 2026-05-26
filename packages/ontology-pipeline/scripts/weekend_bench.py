#!/usr/bin/env python3
"""
Weekend pilot: markdown vs JSON tabular context × LLM models × invocation runtimes.

Models (env): OPENAI, DEEPSEEK, CLAUDE (ANTHROPIC_API_KEY), GEMINI (GOOGLE_API_KEY)
Runtimes:
  native   — vendor SDK (openai / anthropic / google-genai)
  langchain — ChatOpenAI / ChatAnthropic / ChatGoogleGenerativeAI
  skill    — native SDK + procedural skill system prompt (weekend_skill_system.txt)
  foundry  — ontology_foundry OpenAIChatProvider (openai + deepseek only)

Examples:
  python scripts/weekend_bench.py --dry-run
  python scripts/weekend_bench.py --quick
  python scripts/weekend_bench.py --models openai,deepseek --runtimes native,skill
  python scripts/weekend_bench.py --summarize-only --output results/weekend_run.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from weekend_bench.bundle_loader import bundle_from_preview_files, default_preview_paths
from weekend_bench.grade import grade_answer, parse_model_json
from weekend_bench.runners import (
    MODEL_SPECS,
    ChatRunner,
    ModelKind,
    RuntimeKind,
    model_available,
    runtime_available,
)
from weekend_bench.serializers import render_context
from weekend_bench.summarize import load_results, print_summary

DEFAULT_MODELS: list[ModelKind] = ["openai", "claude", "gemini", "deepseek"]
DEFAULT_RUNTIMES: list[RuntimeKind] = ["native", "langchain", "skill", "foundry"]
DEFAULT_FORMATS = ["markdown", "json"]

RESULT_FIELDS = [
    "run_id",
    "timestamp",
    "question_id",
    "model",
    "runtime",
    "format",
    "correct",
    "answer",
    "gold",
    "evidence_column",
    "error",
    "context_chars",
    "latency_ms",
    "model_id",
]


def _load_questions(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row["question_id"],
        row["model"],
        row["runtime"],
        row["format"],
        row.get("model_id", ""),
    )


def _existing_keys(path: Path) -> set[tuple[str, str, str, str, str]]:
    if not path.is_file():
        return set()
    return {_cell_key(r) for r in load_results(path)}


def _append_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekend context-format × model × runtime bench")
    parser.add_argument(
        "--preview-dir",
        type=Path,
        default=Path("output/preview"),
        help="ontology-pipeline preview output root",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=_SCRIPT_DIR / "weekend_questions.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="CSV path (default: results/weekend_<timestamp>.csv)",
    )
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated: openai,claude,gemini,deepseek",
    )
    parser.add_argument(
        "--runtimes",
        default=",".join(DEFAULT_RUNTIMES),
        help="Comma-separated: native,langchain,skill,foundry",
    )
    parser.add_argument(
        "--formats",
        default=",".join(DEFAULT_FORMATS),
        help="Comma-separated: markdown,json",
    )
    parser.add_argument("--max-sample-rows", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Print matrix only")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="2 questions, deepseek+openai, native+skill, both formats",
    )
    parser.add_argument("--resume", action="store_true", help="Skip cells already in output CSV")
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument(
        "--plot",
        action="store_true",
        help="After run, write PNG charts via weekend_plot.py (needs matplotlib)",
    )
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.output = Path("results") / f"weekend_{stamp}.csv"

    if args.summarize_only:
        print_summary(load_results(args.output))
        return 0

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    runtimes = [r.strip() for r in args.runtimes.split(",") if r.strip()]
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]

    if args.quick:
        models = ["deepseek", "openai"]
        runtimes = ["native", "skill"]
        formats = ["markdown", "json"]

    questions = _load_questions(args.questions)
    if args.quick:
        questions = questions[:2]

    agg_path, samples_path = default_preview_paths(args.preview_dir)
    if not agg_path.is_file() or not samples_path.is_file():
        print(f"Missing preview artifacts:\n  {agg_path}\n  {samples_path}", file=sys.stderr)
        return 1

    bundle = bundle_from_preview_files(agg_path, samples_path, max_sample_rows=args.max_sample_rows)
    contexts = {
        fmt: render_context(bundle, fmt, max_sample_rows=args.max_sample_rows)  # type: ignore[arg-type]
        for fmt in formats
    }

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    done_keys = _existing_keys(args.output) if args.resume else set()

    planned: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for model in models:
        mk = model  # type: ignore[assignment]
        if not model_available(mk):  # type: ignore[arg-type]
            skipped.append(f"{model}: no API key")
            continue
        for runtime in runtimes:
            ok, reason = runtime_available(runtime, mk)  # type: ignore[arg-type]
            if not ok:
                skipped.append(f"{model}+{runtime}: {reason}")
                continue
            for fmt in formats:
                planned.append((model, runtime, fmt))

    print(f"Run id: {run_id}")
    print(f"Table: {bundle.table_id}")
    print(f"Questions: {len(questions)}")
    print(f"Planned cells: {len(planned)} × {len(questions)} = {len(planned) * len(questions)} calls")
    if skipped:
        print("Skipped combos:")
        for s in skipped:
            print(f"  - {s}")

    if args.dry_run:
        for model, runtime, fmt in planned:
            print(f"  would run: {model} | {runtime} | {fmt} | {len(contexts[fmt])} chars")
        return 0

    for model, runtime, fmt in planned:
        mk = model  # type: ignore[assignment]
        rt = runtime  # type: ignore[assignment]
        try:
            runner = ChatRunner(model_kind=mk, runtime=rt)
        except RuntimeError as e:
            print(f"skip runner {model}+{runtime}: {e}")
            continue

        context_body = contexts[fmt]
        for q in questions:
            model_id = runner.model_name
            key = (q["id"], model, runtime, fmt, model_id)
            if key in done_keys:
                continue

            row = {
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "question_id": q["id"],
                "model": model,
                "runtime": runtime,
                "format": fmt,
                "correct": "",
                "answer": "",
                "gold": q["gold"],
                "evidence_column": "",
                "error": "",
                "context_chars": str(len(context_body)),
                "latency_ms": "",
                "model_id": model_id,
            }

            t0 = time.perf_counter()
            try:
                raw = runner.complete(context=context_body, question=q["question"])
                parsed = parse_model_json(raw)
                ok, norm = grade_answer(
                    parsed=parsed,
                    gold=q["gold"],
                    kind=q["kind"],  # type: ignore[arg-type]
                )
                row["correct"] = str(ok)
                row["answer"] = norm
                row["evidence_column"] = str(parsed.get("evidence_column", ""))
            except Exception as exc:  # noqa: BLE001
                row["correct"] = "False"
                row["error"] = str(exc)[:500]
            row["latency_ms"] = str(int((time.perf_counter() - t0) * 1000))

            _append_row(args.output, row)
            done_keys.add(key)
            status = "OK" if row["correct"] == "True" else "MISS"
            print(
                f"[{status}] {q['id']} | {model} | {runtime} | {fmt} | "
                f"{row.get('answer', '')[:40]} ({row['latency_ms']}ms)"
            )

    print(f"\nWrote: {args.output}")
    print_summary(load_results(args.output))

    if args.plot:
        import subprocess

        plot_script = _SCRIPT_DIR / "weekend_plot.py"
        rc = subprocess.call(
            [sys.executable, str(plot_script), "--input", str(args.output), "--chart", "all"],
        )
        if rc != 0:
            print("Plot step failed (install matplotlib?).", file=sys.stderr)
            return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
