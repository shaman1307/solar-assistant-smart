#!/usr/bin/env python3
"""Build PV horizon masks from clear days (Influx string power per MPPT).

Run on the Pi:

  .venv/bin/python scripts/build_horizon_mask.py 2026-09-27 2026-10-02 2026-11-08
  .venv/bin/python scripts/build_horizon_mask.py --apply 2026-09-27 2026-10-02 ...

Blocks need an ``mppt`` number (Configuration → PV Blocks). ``--apply`` copies
each MPPT mask into ``horizon_mask`` of every block on that MPPT in the active
template.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config, save_config  # noqa: E402
from src.pv_horizon import MASK_STEP_DEG, assign_block_masks, build_masks  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Build PV horizon masks per block")
    p.add_argument("dates", nargs="+", help="Clear Warsaw days, YYYY-MM-DD")
    p.add_argument("--apply", action="store_true", help="Save masks into the active template")
    args = p.parse_args()

    cfg = load_config()
    if not any(b.get("mppt") is not None for b in cfg["solar"]["blocks"]):
        print("No PV block has an mppt number", file=sys.stderr)
        return 1
    masks, report = build_masks(cfg, args.dates)
    for mppt, mask in masks.items():
        bins = [f"{i * MASK_STEP_DEG}°:{h}" for i, h in enumerate(mask) if h > 0]
        print(f"MPPT{mppt} {report[mppt]} horizon: {' '.join(bins) or 'none'}")
    if args.apply:
        assign_block_masks(cfg, masks)
        cfg["solar"].pop("horizon_masks", None)
        save_config(cfg)
        print("Saved horizon_mask on blocks")
    else:
        print(json.dumps(masks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
