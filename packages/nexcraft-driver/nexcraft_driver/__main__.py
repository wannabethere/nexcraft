"""Entry point: `python -m nexcraft_driver`.

Starts a Flight gRPC server backed by whichever sources have env vars
configured. Bearer-token auth via `NEXCRAFT_DRIVER_TOKENS` (or
`NEXCRAFT_DRIVER_INSECURE=1` for local dev).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from nexcraft_driver.server import build_driver_server


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="nexcraft-driver")
    ap.add_argument("--host", default=os.environ.get("NEXCRAFT_DRIVER_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NEXCRAFT_DRIVER_PORT", "50051")))
    ap.add_argument("--spool-dir", default=os.environ.get("NEXCRAFT_DRIVER_SPOOL", "_async_results"))
    ap.add_argument("--log-level", default=os.environ.get("NEXCRAFT_DRIVER_LOG_LEVEL", "INFO"))
    return ap.parse_args()


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    location = f"grpc://{args.host}:{args.port}"
    server = await build_driver_server(location=location, spool_dir=args.spool_dir)
    logging.info("nexcraft-driver listening on %s (spool=%s)", location, args.spool_dir)

    stop = asyncio.Event()

    def _on_signal(*_):
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows / restricted runtimes — fall back to default handler.
            pass

    # serve() blocks until shutdown() is called; run it in a thread so we can
    # await the stop event.
    serve_task = asyncio.create_task(asyncio.to_thread(server.serve))
    await stop.wait()
    logging.info("shutdown requested")
    server.shutdown()
    await serve_task
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    sys.exit(main())
