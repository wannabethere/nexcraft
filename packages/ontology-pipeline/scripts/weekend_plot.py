#!/usr/bin/env python3
"""
Build LinkedIn-ready PNG charts from weekend_bench CSV output.

Examples:
  python scripts/weekend_plot.py --input results/weekend_run.csv
  python scripts/weekend_plot.py --latest
  python scripts/weekend_plot.py --input results/weekend_run.csv --chart all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from weekend_bench.summarize import group_accuracy, load_results, scored_rows

RESULTS_DIR = Path("results")


def _latest_csv() -> Path | None:
    if not RESULTS_DIR.is_dir():
        return None
    files = sorted(RESULTS_DIR.glob("weekend_*.csv"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _require_matplotlib() -> None:
    import os

    # Headless + writable cache (CI/sandbox safe)
    os.environ.setdefault("MPLCONFIGDIR", str(Path("results").resolve() / ".matplotlib"))
    try:
        import matplotlib

        matplotlib.use("Agg")
    except ImportError as e:
        raise SystemExit(
            "matplotlib is required for plotting. Install: pip install matplotlib"
        ) from e


def _save(fig, path: Path, *, dpi: int = 150) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor="white")
    print(f"Wrote {path}")


def plot_model_by_format(
    rows: list[dict[str, str]],
    out: Path,
    *,
    runtime: str | None = None,
) -> None:
    """Grouped bars: model on x-axis, markdown vs json side by side."""
    _require_matplotlib()
    import matplotlib.pyplot as mp_plt

    subset = rows
    if runtime:
        subset = [r for r in rows if r.get("runtime") == runtime]
    acc = group_accuracy(subset, "model", "format")
    models = sorted({k[0] for k in acc})
    formats = sorted({k[1] for k in acc})
    if not models or not formats:
        raise SystemExit("No scored rows to plot (model × format).")

    x = range(len(models))
    width = 0.35
    offsets = [-width / 2, width / 2] if len(formats) == 2 else [
        (i - (len(formats) - 1) / 2) * width for i in range(len(formats))
    ]

    fig, ax = mp_plt.subplots(figsize=(10, 5.5))
    colors = {"markdown": "#2563eb", "json": "#dc2626"}
    for i, fmt in enumerate(formats):
        vals = [acc.get((m, fmt), 0.0) for m in models]
        ax.bar(
            [xi + offsets[i] for xi in x],
            vals,
            width=width,
            label=fmt,
            color=colors.get(fmt, None),
            edgecolor="white",
            linewidth=0.6,
        )

    title = "Tabular context QA accuracy by model and format"
    if runtime:
        title += f" ({runtime})"
    ax.set_title(title, fontsize=13, fontweight="600", pad=12)
    ax.set_ylabel("Accuracy (%)", fontsize=11)
    ax.set_xlabel("Model", fontsize=11)
    ax.set_xticks(list(x))
    ax.set_xticklabels(models)
    ax.set_ylim(0, 105)
    ax.axhline(100, color="#e5e7eb", linewidth=0.8, zorder=0)
    ax.legend(title="Context format", frameon=True)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    _save(fig, out)
    mp_plt.close(fig)


def plot_runtime_comparison(rows: list[dict[str, str]], out: Path) -> None:
    """Bars: runtime on x-axis (averaged over models and formats)."""
    _require_matplotlib()
    import matplotlib.pyplot as mp_plt

    acc = group_accuracy(rows, "runtime")
    runtimes = [k[0] for k in acc]
    vals = [acc[k] for k in acc]
    if not runtimes:
        raise SystemExit("No scored rows to plot (runtime).")

    fig, ax = mp_plt.subplots(figsize=(8, 5))
    bars = ax.bar(runtimes, vals, color="#0d9488", edgecolor="white", linewidth=0.6)
    ax.bar_label(bars, labels=[f"{v:.1f}%" for v in vals], padding=4, fontsize=10)
    ax.set_title(
        "Accuracy by invocation runtime (avg over models & formats)",
        fontsize=13,
        fontweight="600",
        pad=12,
    )
    ax.set_ylabel("Accuracy (%)", fontsize=11)
    ax.set_ylim(0, 105)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    _save(fig, out)
    mp_plt.close(fig)


def plot_overview(rows: list[dict[str, str]], out: Path) -> None:
    """2×2 dashboard for paper / LinkedIn carousel."""
    _require_matplotlib()
    import matplotlib.pyplot as mp_plt

    fig, axes = mp_plt.subplots(2, 2, figsize=(12, 9))
    fig.suptitle(
        "Weekend pilot: Markdown vs JSON context for LLM tabular QA",
        fontsize=14,
        fontweight="600",
        y=0.98,
    )

    panels = [
        (axes[0, 0], group_accuracy(rows, "format"), "By context format"),
        (axes[0, 1], group_accuracy(rows, "model"), "By model"),
        (axes[1, 0], group_accuracy(rows, "runtime"), "By runtime"),
        (axes[1, 1], group_accuracy(rows, "model", "format"), "By model × format"),
    ]
    colors_fmt = {"markdown": "#2563eb", "json": "#dc2626"}

    for ax, acc, title in panels:
        if not acc:
            ax.set_visible(False)
            continue
        labels: list[str] = []
        vals: list[float] = []
        bar_colors: list[str] = []
        for key, v in acc.items():
            if isinstance(key, tuple):
                label = " | ".join(key)
                bar_colors.append(
                    colors_fmt.get(key[1], "#64748b") if len(key) == 2 else "#64748b"
                )
            else:
                label = str(key)
                bar_colors.append(colors_fmt.get(label, "#64748b"))
            labels.append(label)
            vals.append(v)
        bars = ax.bar(range(len(labels)), vals, color=bar_colors, edgecolor="white")
        ax.bar_label(bars, labels=[f"{v:.0f}%" for v in vals], padding=2, fontsize=8)
        ax.set_title(title, fontsize=11, fontweight="600")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8)
        ax.set_ylim(0, 105)
        ax.grid(axis="y", alpha=0.2)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    _save(fig, out, dpi=160)
    mp_plt.close(fig)


def plot_runtime_facets(rows: list[dict[str, str]], out: Path) -> None:
    """One panel per runtime: model × format grouped bars."""
    _require_matplotlib()
    import matplotlib.pyplot as mp_plt

    runtimes = sorted({r.get("runtime", "") for r in rows if r.get("runtime")})
    if not runtimes:
        raise SystemExit("No runtimes in results.")

    n = len(runtimes)
    cols = 2 if n > 1 else 1
    rows_n = (n + cols - 1) // cols
    fig, axes = mp_plt.subplots(rows_n, cols, figsize=(6 * cols, 4.5 * rows_n))
    axes_flat = axes.flatten() if hasattr(axes, "flatten") else [axes]

    colors = {"markdown": "#2563eb", "json": "#dc2626"}
    for idx, rt in enumerate(runtimes):
        ax = axes_flat[idx]
        subset = [r for r in rows if r.get("runtime") == rt]
        acc = group_accuracy(subset, "model", "format")
        models = sorted({k[0] for k in acc})
        formats = sorted({k[1] for k in acc})
        x = range(len(models))
        width = 0.35
        for fi, fmt in enumerate(formats):
            vals = [acc.get((m, fmt), 0.0) for m in models]
            offset = (fi - (len(formats) - 1) / 2) * width
            ax.bar(
                [xi + offset for xi in x],
                vals,
                width=width,
                label=fmt,
                color=colors.get(fmt),
            )
        ax.set_title(rt, fontweight="600")
        ax.set_xticks(list(x))
        ax.set_xticklabels(models, fontsize=9)
        ax.set_ylim(0, 105)
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.2)

    for j in range(len(runtimes), len(axes_flat)):
        axes_flat[j].set_visible(False)

    fig.suptitle("Accuracy by runtime (model × format)", fontsize=13, fontweight="600")
    fig.tight_layout()
    _save(fig, out, dpi=160)
    mp_plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot weekend bench CSV results")
    parser.add_argument("--input", type=Path, default=None, help="Results CSV from weekend_bench.py")
    parser.add_argument("--latest", action="store_true", help="Use newest results/weekend_*.csv")
    parser.add_argument(
        "--chart",
        choices=("overview", "model_format", "runtime", "facets", "all"),
        default="all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for PNGs (default: same dir as input CSV)",
    )
    parser.add_argument("--runtime", default=None, help="Filter model_format chart to one runtime")
    args = parser.parse_args()

    csv_path = args.input
    if args.latest:
        csv_path = _latest_csv()
        if csv_path is None:
            print("No results/weekend_*.csv found.", file=sys.stderr)
            return 1
    if csv_path is None or not csv_path.is_file():
        print("Provide --input or --latest.", file=sys.stderr)
        return 1

    rows = scored_rows(load_results(csv_path))
    if not rows:
        print(f"No graded rows in {csv_path}", file=sys.stderr)
        return 1

    out_dir = args.output_dir or csv_path.parent
    stem = csv_path.stem

    charts = {
        "overview": (plot_overview, out_dir / f"{stem}_overview.png"),
        "model_format": (
            lambda r, p: plot_model_by_format(r, p, runtime=args.runtime),
            out_dir / f"{stem}_model_format.png",
        ),
        "runtime": (plot_runtime_comparison, out_dir / f"{stem}_runtime.png"),
        "facets": (plot_runtime_facets, out_dir / f"{stem}_facets.png"),
    }

    to_run = list(charts.keys()) if args.chart == "all" else [args.chart]
    for name in to_run:
        fn, path = charts[name]
        fn(rows, path)

    print(f"Plotted {len(rows)} graded rows from {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
