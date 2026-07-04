"""
Testes do loader incremental (loader.py)
Foco: watermark, idempotência, tratamento de falhas, resumo correto.
Usa mocks para não fazer chamadas reais à API do HN.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from parte2_hacker_news.database import get_connection, get_state, init_db, STATE_LAST_ID
from parte2_hacker_news.loader import run_load, fetch_item, LoadSummary


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    db_path.unlink(missing_ok=True)


def _make_item(item_id: int, itype: str = "story") -> dict:
    return {
        "id": item_id,
        "type": itype,
        "by": "user",
        "time": 1700000000 + item_id,
        "title": f"Story {item_id}",
        "score": 10,
    }


# ---------------------------------------------------------------------------
# Testes de watermark e idempotência
# ---------------------------------------------------------------------------

def test_primeira_execucao_define_watermark(tmp_db):
    """Após a 1ª execução, o watermark deve ser salvo na base."""
    items = {i: _make_item(i) for i in range(900, 911)}  # IDs 900–910

    def mock_fetch_item(session, iid):
        return items.get(iid), None

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=910), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch_item):
        run_load(initial_batch_size=11, db_path=tmp_db)

    conn = get_connection(tmp_db)
    watermark = get_state(conn, STATE_LAST_ID)
    conn.close()
    assert watermark == "910"


def test_segunda_execucao_usa_apenas_ids_novos(tmp_db):
    """Na 2ª execução, fetch_item só deve ser chamado para IDs acima do watermark."""
    items = {i: _make_item(i) for i in range(900, 916)}

    def mock_fetch(session, iid):
        return items.get(iid), None

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=910), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch):
        run_load(initial_batch_size=11, db_path=tmp_db)

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=915), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch) as mock_fi:
        run_load(initial_batch_size=11, db_path=tmp_db)

    called_ids = [c.args[1] for c in mock_fi.call_args_list]
    # Apenas IDs 911–915 devem ter sido consultados
    assert all(iid >= 911 for iid in called_ids)
    assert set(called_ids) == {911, 912, 913, 914, 915}


def test_reexecucao_sem_novos_ids_nao_modifica_watermark(tmp_db):
    """Se max_id == last_id, não há nada a processar e o watermark não muda."""
    items = {i: _make_item(i) for i in range(900, 906)}

    def mock_fetch(session, iid):
        return items.get(iid), None

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=905), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch):
        run_load(initial_batch_size=6, db_path=tmp_db)

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=905), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch) as mock_fi:
        run_load(db_path=tmp_db)

    # Nenhuma chamada à API de item deve ter ocorrido
    assert mock_fi.call_count == 0

    conn = get_connection(tmp_db)
    assert get_state(conn, STATE_LAST_ID) == "905"
    conn.close()


# ---------------------------------------------------------------------------
# Testes de tratamento de falhas
# ---------------------------------------------------------------------------

def test_item_nulo_contabilizado_como_ignorado(tmp_db):
    """Item null da API (deletado) deve ser ignorado, não inserido."""
    def mock_fetch(session, iid):
        return None, None  # null → item deletado

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=902), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch):
        summary = run_load(initial_batch_size=3, db_path=tmp_db)

    assert summary.ignored == 3
    assert summary.inserted == 0

    conn = get_connection(tmp_db)
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    assert count == 0


def test_item_com_erro_vai_para_fila_de_falhas(tmp_db):
    """IDs que retornam erro devem ser adicionados à failed_items."""
    def mock_fetch(session, iid):
        if iid == 901:
            return None, "timeout"
        return _make_item(iid), None

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=903), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch):
        summary = run_load(initial_batch_size=4, db_path=tmp_db)

    assert summary.failures >= 1

    conn = get_connection(tmp_db)
    failed = conn.execute("SELECT id FROM failed_items").fetchall()
    failed_ids = [r["id"] for r in failed]
    conn.close()
    assert 901 in failed_ids


def test_retry_de_ids_falhos(tmp_db):
    """IDs em failed_items devem ser reprocessados na próxima execução."""
    call_count = {"901": 0}

    def mock_fetch_first(session, iid):
        return None, "timeout"  # Tudo falha na 1ª execução

    def mock_fetch_second(session, iid):
        call_count[str(iid)] = call_count.get(str(iid), 0) + 1
        return _make_item(iid), None  # Sucesso na 2ª

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=900), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch_first):
        run_load(initial_batch_size=1, db_path=tmp_db)

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=900), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch_second):
        summary = run_load(initial_batch_size=1, db_path=tmp_db)

    assert summary.retried_ok >= 1

    conn = get_connection(tmp_db)
    count = conn.execute("SELECT COUNT(*) FROM items WHERE id=900").fetchone()[0]
    conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# Teste de resumo
# ---------------------------------------------------------------------------

def test_summary_campos(tmp_db):
    items = {i: _make_item(i) for i in range(100, 105)}

    def mock_fetch(session, iid):
        return items.get(iid), None

    with patch("parte2_hacker_news.loader.fetch_max_item", return_value=104), \
         patch("parte2_hacker_news.loader.fetch_item", side_effect=mock_fetch):
        summary = run_load(initial_batch_size=5, db_path=tmp_db)

    assert summary.inserted == 5
    assert summary.failures == 0
    assert summary.duration_seconds > 0
    assert summary.start_id == 100
    assert summary.end_id == 104
