"""
Camada de persistência — SQLite
================================
Schema:
  items       — dados dos itens do HN (chave: id INTEGER)
  state       — watermark e config incremental (chave: key TEXT)
  failed_items — IDs que falharam na última tentativa; reprocessados
                 no próximo ciclo antes de avançar para novos IDs.

Decisão: SQLite foi escolhido pela simplicidade de execução em máquina
limpa (zero configuração), suporte nativo ao Python e adequação ao
volume esperado. Troca: sem suporte a múltiplos processos simultâneos
com alto throughput; para escala maior, migrar para PostgreSQL.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "hackernews.db"

_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    type        TEXT,
    by          TEXT,
    time        INTEGER,
    title       TEXT,
    url         TEXT,
    score       INTEGER,
    descendants INTEGER,
    raw_json    TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failed_items (
    id           INTEGER PRIMARY KEY,
    error_msg    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1,
    last_attempt TEXT    NOT NULL
);
"""

STATE_LAST_ID = "last_item_id"


# ---------------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=MEMORY")  # evita arquivos de journal em disco
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for statement in _DDL.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# State (watermark)
# ---------------------------------------------------------------------------

def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = _now()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def upsert_item(conn: sqlite3.Connection, item: dict) -> str:
    """
    Insere ou atualiza um item. Retorna 'inserted' | 'updated'.
    Idempotente: chamar duas vezes com o mesmo item não duplica registros.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id FROM items WHERE id = ?", (item["id"],)
    ).fetchone()

    params = (
        item.get("type"),
        item.get("by"),
        item.get("time"),
        item.get("title"),
        item.get("url"),
        item.get("score"),
        item.get("descendants"),
        json.dumps(item, ensure_ascii=False),
        now,
        item["id"],
    )

    if existing:
        conn.execute(
            """UPDATE items
               SET type=?, by=?, time=?, title=?, url=?, score=?,
                   descendants=?, raw_json=?, fetched_at=?
               WHERE id=?""",
            params,
        )
        conn.commit()
        return "updated"

    conn.execute(
        """INSERT INTO items
               (type, by, time, title, url, score, descendants, raw_json, fetched_at, id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        params,
    )
    conn.commit()
    return "inserted"


# ---------------------------------------------------------------------------
# Failed items (retry queue)
# ---------------------------------------------------------------------------

def add_failed(conn: sqlite3.Connection, item_id: int, error_msg: str) -> None:
    now = _now()
    existing = conn.execute(
        "SELECT attempts FROM failed_items WHERE id = ?", (item_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE failed_items SET attempts=?, last_attempt=?, error_msg=? WHERE id=?",
            (existing["attempts"] + 1, now, error_msg, item_id),
        )
    else:
        conn.execute(
            "INSERT INTO failed_items (id, error_msg, attempts, last_attempt) VALUES (?,?,1,?)",
            (item_id, error_msg, now),
        )
    conn.commit()


def remove_failed(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM failed_items WHERE id = ?", (item_id,))
    conn.commit()


def get_failed_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT id FROM failed_items ORDER BY id").fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
