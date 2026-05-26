"""CLI: run Temporal workflows from YAML job definitions."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from nexcraft_jobs.yaml_jobs.loader import load_job_file
from nexcraft_jobs.yaml_jobs.runner import run_job_spec


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    run_one = sub.add_parser("run", help="Run a single job YAML file.")
    run_one.add_argument("job_file", type=Path, help="Path to .yaml job definition.")
    run_one.add_argument(
        "--temporal-target",
        default=None,
        help="Override temporal_target from YAML (also env TEMPORAL_TARGET).",
    )
    run_one.add_argument(
        "--no-wait",
        action="store_true",
        help="Start workflow and exit without waiting for result.",
    )

    run_dir = sub.add_parser("run-dir", help="Run every *.yaml / *.yml file in a directory (sorted).")
    run_dir.add_argument("directory", type=Path)
    run_dir.add_argument("--temporal-target", default=None)
    run_dir.add_argument("--no-wait", action="store_true")
    run_dir.add_argument("--fail-fast", action="store_true", help="Stop on first failing job.")

    return p.parse_args(argv)


async def _cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    spec = load_job_file(args.job_file)
    if args.no_wait:
        spec = spec.model_copy(update={"wait_for_result": False})
    temporal_target = args.temporal_target or os.environ.get("TEMPORAL_TARGET")
    try:
        out = await run_job_spec(spec, temporal_target_override=temporal_target)
    except Exception:
        logging.exception("Job failed")
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


async def _cmd_run_dir(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    temporal_target = args.temporal_target or os.environ.get("TEMPORAL_TARGET")
    paths = sorted(args.directory.glob("*.yaml")) + sorted(args.directory.glob("*.yml"))
    if not paths:
        logging.error("No *.yaml / *.yml files in %s", args.directory)
        return 1

    results: list[dict] = []
    for path in paths:
        logging.info("--- Job file: %s", path)
        spec = load_job_file(path)
        if args.no_wait:
            spec = spec.model_copy(update={"wait_for_result": False})
        try:
            out = await run_job_spec(spec, temporal_target_override=temporal_target)
            results.append({"file": str(path), "ok": True, "result": out})
        except Exception as exc:
            logging.exception("Job %s failed", path)
            results.append({"file": str(path), "ok": False, "error": str(exc)})
            if args.fail_fast:
                print(json.dumps(results, indent=2, default=str))
                return 1

    print(json.dumps(results, indent=2, default=str))
    return 0 if all(r["ok"] for r in results) else 1


async def main_async(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "run":
        return await _cmd_run(args)
    if args.command == "run-dir":
        return await _cmd_run_dir(args)
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(asyncio.run(main_async(argv)))


if __name__ == "__main__":
    main()
