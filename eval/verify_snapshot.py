"""Verify a redacted evaluation snapshot from its case-level evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.verified_snapshot import verify_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify an AI Hair Salon evaluation snapshot."
    )
    parser.add_argument("snapshot", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
        verify_snapshot(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"snapshot verification failed: {exc}", file=sys.stderr)
        return 1
    print("snapshot verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
