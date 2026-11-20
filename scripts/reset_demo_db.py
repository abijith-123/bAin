#!/usr/bin/env python3
"""Create a fresh bAin database containing only the reproducible synthetic cases."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    default_path = Path(os.environ.get("BAIN_DB_PATH", Path(tempfile.gettempdir()) / "bain-demo.sqlite3"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=default_path,
        help=f"SQLite path to reset (default: {default_path})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Confirm deleting the selected existing database and its SQLite WAL files.",
    )
    args = parser.parse_args()
    database_path = args.database.expanduser().resolve()

    if database_path.exists() and not args.force:
        parser.error(f"{database_path} exists; pass --force to replace this synthetic demo database")

    os.environ["BAIN_DB_PATH"] = str(database_path)
    from backend import main as api  # Import after setting the selected database path.

    database_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")):
        if path.exists():
            path.unlink()

    api.DB_PATH = database_path
    api.init_db()
    print(f"Fresh synthetic bAin database initialized at: {database_path}")
    print(f"Seeded cases: {', '.join(case['patient_id'] for case in api.seed_cases())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
