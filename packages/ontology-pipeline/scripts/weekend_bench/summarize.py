from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


def load_results(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scored_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get("correct", "").lower() in ("true", "false")]


def accuracy_pct(group: list[dict[str, str]]) -> float:
    if not group:
        return 0.0
    ok = sum(1 for r in group if r.get("correct", "").lower() == "true")
    return 100.0 * ok / len(group)


def group_accuracy(
    rows: list[dict[str, str]],
    *key_fields: str,
) -> dict[tuple[str, ...], float]:
    """Map grouping key → accuracy %."""
    buckets: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        key = tuple(r[k] for k in key_fields)
        buckets[key].append(r)
    return {k: accuracy_pct(v) for k, v in sorted(buckets.items())}


def print_summary(rows: list[dict[str, str]]) -> None:
    if not rows:
        print("No results to summarize.")
        return

    scored = [r for r in rows if r.get("correct") in ("True", "False", "true", "false")]
    if not scored:
        print(f"{len(rows)} rows (no graded rows yet).")
        return

    def _acc(group: list[dict[str, str]]) -> str:
        ok = sum(1 for r in group if r.get("correct", "").lower() == "true")
        return f"{100.0 * ok / len(group):.1f}% ({ok}/{len(group)})"

    by_format: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_model: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_runtime: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_cell: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)

    for r in scored:
        by_format[r["format"]].append(r)
        by_model[r["model"]].append(r)
        by_runtime[r["runtime"]].append(r)
        by_cell[(r["model"], r["format"], r["runtime"])].append(r)

    print("\n=== Weekend bench summary ===\n")
    print(f"Graded rows: {len(scored)}\n")

    print("By context format:")
    for fmt in sorted(by_format):
        print(f"  {fmt:10} {_acc(by_format[fmt])}")

    print("\nBy model:")
    for m in sorted(by_model):
        print(f"  {m:10} {_acc(by_model[m])}")

    print("\nBy runtime (native / langchain / skill / foundry):")
    for rt in sorted(by_runtime):
        print(f"  {rt:10} {_acc(by_runtime[rt])}")

    print("\nBy model × format × runtime:")
    for key in sorted(by_cell):
        m, fmt, rt = key
        print(f"  {m} | {fmt} | {rt:10} {_acc(by_cell[key])}")
