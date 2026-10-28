"""Create a broken vendor-feed CSV for the next day and record the fault in ground_truth.

Run from the project root:
    python -m scripts.inject_fault currency_unit                 # one fault
    python -m scripts.inject_fault currency_unit+null_spike      # combo: two faults at once
    python -m scripts.inject_fault timezone_shift 2026-10-29     # optional date (default 2026-10-28)

ground_truth is for evals only. Agents must NEVER read it.
"""

import sys

from app.faults import FAULTS
from app.injector import create_feed

DEFAULT_DAY = "2026-10-28"  # the day after the 7 clean days

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.inject_fault <fault>[+<fault>] [YYYY-MM-DD]")
        print("Faults:", ", ".join(FAULTS))
        sys.exit(1)

    names = sys.argv[1].split("+")
    day = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DAY
    unknown = [n for n in names if n not in FAULTS]
    if unknown:
        sys.exit(f"Unknown fault(s): {unknown}. Choose from: {', '.join(FAULTS)}")

    made = create_feed(day, names)
    print(f"Clean day had {made['clean_rows']} rows, broken feed has {made['feed_rows']} rows")
    print(f"Wrote {made['path']}")
    print(f"Ground truth saved: {'+'.join(names)} -> {made['details']}")
