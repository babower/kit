"""`opsreport status` and `opsreport flush`, for cron jobs and checking on a buffer."""

from __future__ import annotations

import argparse
import logging
import sys

from .reporter import Reporter


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsreport")
    p.add_argument("--project", help="project slug (default: OPS_PROJECT)")
    p.add_argument("--buffer-dir", help="buffer directory (default: OPS_BUFFER_DIR or .opsreport)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is reporting on, and what is waiting?")
    sub.add_parser("flush", help="send everything that is buffered")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ops = Reporter(args.project, buffer_dir=args.buffer_dir)
    if args.cmd == "status":
        state = "on" if ops.enabled else "OFF (needs a project, OPS_URL and OPS_TOKEN)"
        print(f"reporting: {state}")
        print(f"project:   {ops.project or '-'}")
        print(f"url:       {ops.url or '-'}")
        print(f"buffered:  {ops.buffer.pending()} waiting, {ops.buffer.rejected()} refused")
        return 0
    result = ops.flush()
    print(f"sent {result.sent} ({result.duplicates} already had), {result.pending} still "
          f"buffered, {result.rejected} refused" + (f", stopped: {result.error}"
                                                     if result.error else ""))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
