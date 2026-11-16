"""Write a database backup now: python scripts/backup_db.py"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.session import init_db  # noqa: E402
from services.backup import run_backup  # noqa: E402

if __name__ == "__main__":
    init_db()
    path = run_backup(force=True)
    if path is None:
        sys.exit("Sauvegarde impossible : consultez les journaux.")
    print(path)
