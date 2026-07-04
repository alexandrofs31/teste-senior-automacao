"""
Testes da camada de persistência (database.py)
Foco: idempotência, watermark, itens nulos, fila de falhas.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from parte2_hacker_news.database import (
    add_failed,
    get_connection,
    get_failed_ids,
    get_state,
    init_db,
    remove_failed,
    set_state,
    upsert_item,
)


@pytest.fixture
def conn():
    """Banco em memória para cada teste — isolado e rápido."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    c = get_connection(db_path)
    init_db(c)
    yield c
    c.close()
    db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# State / watermark
# ---------------------------------------------------------------------------

def test_state_inexistente_retorna_none(conn):
    assert get_state(conn, "qualquer_chave") is None


def test_state_set_e_get(conn):
    set_state(conn, "last_item_id", "42000")
    assert get_state(conn, "last_item_id") == "42000"


def test_state_sobrescreve(conn):
    set_state(conn, "last_item_id", "100")
    set_state(conn, "last_item_id", "200")
    assert get_state(conn, "last_item_id") == "200"


# ---------------------------------------------------------------------------
# Idempotência de upsert
# ---------------------------------------------------------------------------

ITEM_BASE = {
    "id": 1,
    "type": "story",
    "by": "pg",
    "time": 1700000000,
    "title": "Hello HN",
    "url": "https://example.com",
    "score": 100,
    "descendants": 5,
}


def test_upsert_insere_novo_item(conn):
    action = upsert_item(conn, ITEM_BASE)
    assert action == "inserted"
    count = conn.execute("SELECT COUNT(*) FROM items WHERE id=1").fetchone()[0]
    assert count == 1


def test_upsert_duplo_nao_duplica(conn):
    upsert_item(conn, ITEM_BASE)
    upsert_item(conn, ITEM_BASE)
    count = conn.execute("SELECT COUNT(*) FROM items WHERE id=1").fetchone()[0]
    assert count == 1


def test_upsert_duplo_retorna_updated(conn):
    upsert_item(conn, ITEM_BASE)
    action = upsert_item(conn, ITEM_BASE)
    assert action == "updated"


def test_upsert_atualiza_score(conn):
    upsert_item(conn, ITEM_BASE)
    updated = {**ITEM_BASE, "score": 999}
    upsert_item(conn, updated)
    row = conn.execute("SELECT score FROM items WHERE id=1").fetchone()
    assert row["score"] == 999


def test_upsert_preserva_raw_json(conn):
    import json
    upsert_item(conn, ITEM_BASE)
    row = conn.execute("SELECT raw_json FROM items WHERE id=1").fetchone()
    parsed = json.loads(row["raw_json"])
    assert parsed["id"] == 1
    assert parsed["title"] == "Hello HN"


def test_upsert_item_campos_opcionais_nulos(conn):
    """Itens com campos faltando não devem lançar exceção."""
    item = {"id": 99, "type": "comment"}
    action = upsert_item(conn, item)
    assert action == "inserted"


# ---------------------------------------------------------------------------
# Fila de falhas
# ---------------------------------------------------------------------------

def test_add_failed_e_listagem(conn):
    add_failed(conn, 101, "timeout")
    ids = get_failed_ids(conn)
    assert 101 in ids


def test_remove_failed(conn):
    add_failed(conn, 202, "http_error")
    remove_failed(conn, 202)
    assert 202 not in get_failed_ids(conn)


def test_add_failed_incrementa_tentativas(conn):
    add_failed(conn, 303, "err1")
    add_failed(conn, 303, "err2")
    row = conn.execute(
        "SELECT attempts FROM failed_items WHERE id=303"
    ).fetchone()
    assert row["attempts"] == 2


def test_failed_ids_ordenados(conn):
    add_failed(conn, 300, "e")
    add_failed(conn, 100, "e")
    add_failed(conn, 200, "e")
    assert get_failed_ids(conn) == [100, 200, 300]
