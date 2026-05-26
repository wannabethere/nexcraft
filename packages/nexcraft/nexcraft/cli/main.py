import argparse
import asyncio


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexcraft")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print version")
    args = parser.parse_args()
    if args.cmd == "version":
        asyncio.run(_print_version())


async def _print_version() -> None:
    try:
        import importlib.metadata as m

        v = m.version("nexcraft")
    except Exception:
        v = "0.1.0"
    print(v)


if __name__ == "__main__":
    main()
