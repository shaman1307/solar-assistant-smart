#!/usr/bin/env python3
"""CLI for Influx datapatch (write / delete / linear fill).

Run on the Pi (or via SSH tunnel to :8086):

  python scripts/influx_datapatch.py drop-range \\
      --measurement "Battery state of charge" \\
      --from 2026-09-11T07:09:00Z --to 2026-09-11T07:10:15Z \\
      --min-value 40

  python scripts/influx_datapatch.py write \\
      --measurement "Battery state of charge" \\
      --time 2026-09-11T07:09:59Z --value 25 --integer

  python scripts/influx_datapatch.py fill-gap \\
      --measurement "PV power" \\
      --from 2026-09-11T12:00:00Z --to 2026-09-11T12:15:00Z \\
      --step-min 5 --integer
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.influx_datapatch import (  # noqa: E402
    drop_values_in_range,
    fill_linear_gap,
    parse_utc,
    write_point,
)

log = logging.getLogger("influx_datapatch")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Patch Solar Assistant Influx points")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Write one combined-field sample")
    w.add_argument("--measurement", required=True)
    w.add_argument("--time", required=True, help="UTC instant, ...Z")
    w.add_argument("--value", required=True, type=float)
    w.add_argument("--integer", action="store_true")

    d = sub.add_parser("drop-range", help="Delete samples in a time range by value")
    d.add_argument("--measurement", required=True)
    d.add_argument("--from", dest="start", required=True)
    d.add_argument("--to", dest="end", required=True)
    d.add_argument("--min-value", type=float, default=None,
                   help="Delete values >= this")
    d.add_argument("--max-value", type=float, default=None,
                   help="Delete values <= this")

    f = sub.add_parser("fill-gap", help="Linear fill using neighbors outside the gap")
    f.add_argument("--measurement", required=True)
    f.add_argument("--from", dest="start", required=True)
    f.add_argument("--to", dest="end", required=True)
    f.add_argument("--step-min", type=float, default=10.0)
    f.add_argument("--integer", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)
    if args.cmd == "write":
        write_point(
            args.measurement, args.value, parse_utc(args.time),
            integer=args.integer,
        )
        log.info("wrote %s %s @ %s", args.measurement, args.value, args.time)
        return 0
    if args.cmd == "drop-range":
        n = drop_values_in_range(
            args.measurement, parse_utc(args.start), parse_utc(args.end),
            min_value=args.min_value, max_value=args.max_value,
        )
        log.info("dropped %s", n)
        return 0
    if args.cmd == "fill-gap":
        n = fill_linear_gap(
            args.measurement,
            parse_utc(args.start),
            parse_utc(args.end),
            timedelta(minutes=args.step_min),
            integer=args.integer,
        )
        log.info("filled %s", n)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
