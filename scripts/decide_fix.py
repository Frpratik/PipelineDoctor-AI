"""Approve or reject a pending fix. Approving is the ONLY thing that changes real data.

Run from the project root:
    python -m scripts.decide_fix approve 1
    python -m scripts.decide_fix reject 1
"""

import sys

from app.fixes import approve_fix, reject_fix

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("approve", "reject"):
        sys.exit("Usage: python -m scripts.decide_fix approve|reject <fix_id>")
    action, fix_id = sys.argv[1], int(sys.argv[2])
    print(approve_fix(fix_id) if action == "approve" else reject_fix(fix_id))
