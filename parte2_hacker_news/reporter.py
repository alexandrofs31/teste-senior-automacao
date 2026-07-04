"""
Reporter — Relatório do estado atual da base
=============================================
Exibe estatísticas sobre o banco de dados local e o watermark corrente.

Uso:
    python -m parte2_hacker_news.reporter
"""

import sqlite3
from pathlib import Path

from .database import DB_PATH, STATE_LAST_ID, get_connection, get_state


def report(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print("Base de dados ainda não criada. Execute o loader primeiro.")
        return

    conn = get_connection(db_path)

    last_id = get_state(conn, STATE_LAST_ID)
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    by_type = conn.execute(
        "SELECT COALESCE(type,'null') AS type, COUNT(*) AS cnt FROM items GROUP BY type ORDER BY cnt DESC"
    ).fetchall()
    failed = conn.execute("SELECT COUNT(*) FROM failed_items").fetchone()[0]
    first_id = conn.execute("SELECT MIN(id) FROM items").fetchone()[0]
    last_db_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]

    conn.close()

    print("=" * 50)
    print("RELATÓRIO DO BANCO LOCAL — HACKER NEWS")
    print("=" * 50)
    print(f"  Watermark (last_item_id) : {last_id or 'não definido'}")
    print(f"  Total de itens           : {total}")
    print(f"  Menor ID na base         : {first_id}")
    print(f"  Maior ID na base         : {last_db_id}")
    print(f"  IDs com falha pendente   : {failed}")
    print()
    print("  Por tipo:")
    for row in by_type:
        print(f"    {row['type']:20s}: {row['cnt']}")
    print("=" * 50)


def main() -> None:
    report()


if __name__ == "__main__":
    main()
