#!/usr/bin/env python3
"""Migration script to create orchestrator tables (investigations, investigation_logs, shared_iocs).

Safe to run multiple times -- create_all() is additive and will not
modify existing tables.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.connection import init_database, get_db_manager


def main():
    print("=" * 50)
    print("Orchestrator Database Migration")
    print("=" * 50)

    try:
        print("\nInitializing database connection...")
        init_database(create_tables=True)
        print("  Tables created/verified successfully")

        db = get_db_manager()
        session = db.get_session()

        from sqlalchemy import inspect
        inspector = inspect(db._engine)
        tables = inspector.get_table_names()

        target_tables = ["investigations", "investigation_logs", "shared_iocs"]
        print("\nVerifying orchestrator tables:")
        all_ok = True
        for t in target_tables:
            exists = t in tables
            status = "OK" if exists else "MISSING"
            print(f"  {t}: {status}")
            if not all_ok:
                all_ok = False

        if all_ok:
            for t in target_tables:
                cols = [c["name"] for c in inspector.get_columns(t)]
                print(f"\n  {t} columns: {', '.join(cols)}")

        session.close()
        print("\nMigration complete!")

    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  cd docker && docker compose up -d postgres")
        sys.exit(1)


if __name__ == "__main__":
    main()
