#!/usr/bin/env python3
"""Validate compose sidecars use read-only root filesystems."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common.compose_security import (  # noqa: E402
    ComposeSecurityError,
    validate_compose_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compose_file", help="Path to docker-compose.yml")
    args = parser.parse_args()

    try:
        sidecars = validate_compose_file(args.compose_file)
    except ComposeSecurityError as exc:
        print(f"sidecar compose validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"validated read-only sidecars: {', '.join(sidecars)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
