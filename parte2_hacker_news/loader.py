"""
Parte 2 — Carga incremental Hacker News
=========================================
Controla o ciclo completo: busca de IDs novos, fetch com retry/backoff,
persistência idempotente e atualização do watermark.

Fluxo incremental:
  1ª execução  → busca os últimos `initial_batch_size` itens (default 200).
  2ª+ execução → busca apenas IDs > last_item_id salvo em state.

  Adicionalmente, antes de processar IDs novos, reprocessa IDs que
  falharam em execuções anteriores (tabela failed_items).

Watermark:
  Avança até o maior ID processado com sucesso (incluindo itens nulos,
  que são itens deletados e por definição "processados"). IDs que
  retornarem erro de rede/HTTP ficam em failed_items e NÃO bloqueiam
  o avanço do watermark — trade-off explícito: preferimos avançar e
  ter uma fila de retentativas em vez de travar o pipeline todo.

Uso:
    # Carga inicial (200 itens)
    python -m parte2_hacker_news.loader

    # Carga inicial com tamanho customizado
    python -m parte2_hacker_news.loader --batch-size 500

    # Carga incremental (detectada automaticamente)
    python -m parte2_hacker_news.loader
"""

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .database import (
    DB_PATH,
    STATE_LAST_ID,
    add_failed,
    get_connection,
    get_failed_ids,
    get_state,
    init_db,
    remove_failed,
    set_state,
    upsert_item,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://hacker-news.firebaseio.com/v0"
DEFAULT_BATCH_SIZE = 200
REQUEST_TIMEOUT = 10  # segundos


# ---------------------------------------------------------------------------
# Sumário de execução
# ---------------------------------------------------------------------------

@dataclass
class LoadSummary:
    start_id: int
    end_id: int
    consulted: int = 0
    inserted: int = 0
    updated: int = 0
    ignored: int = 0   # itens null (deletados)
    failures: int = 0
    retried_ok: int = 0
    duration_seconds: float = 0.0

    def report(self) -> str:
        lines = [
            "=" * 50,
            "RESUMO DA CARGA",
            "=" * 50,
            f"  Faixa processada : {self.start_id} → {self.end_id}",
            f"  Consultados      : {self.consulted}",
            f"  Inseridos        : {self.inserted}",
            f"  Atualizados      : {self.updated}",
            f"  Ignorados (null) : {self.ignored}",
            f"  Falhas           : {self.failures}",
            f"  Retriados (OK)   : {self.retried_ok}",
            f"  Duração          : {self.duration_seconds:.2f}s",
            "=" * 50,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP Session com retry automático
# ---------------------------------------------------------------------------

def make_session(max_retries: int = 3, backoff_factor: float = 0.5) -> requests.Session:
    """
    Cria session com retry automático para erros 5xx e timeouts de rede.
    Backoff exponencial: 0s, 1s, 2s entre tentativas.
    Erros 4xx (ex: 404) não são retentados — falha rápida intencional.
    """
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Chamadas à API
# ---------------------------------------------------------------------------

def fetch_max_item(session: requests.Session) -> int:
    resp = session.get(f"{BASE_URL}/maxitem.json", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return int(resp.json())


def fetch_item(session: requests.Session, item_id: int) -> tuple[Optional[dict], Optional[str]]:
    """
    Busca um item pelo ID.
    Retorna (item_dict, None) em sucesso, (None, error_msg) em falha.
    item_dict pode ser None quando o item foi deletado da API (normal).
    """
    try:
        resp = session.get(
            f"{BASE_URL}/item/{item_id}.json",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()  # None se item deletado
        return data, None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection_error: {e}"
    except requests.exceptions.HTTPError as e:
        return None, f"http_error: {e}"
    except Exception as e:
        return None, f"unexpected: {e}"


# ---------------------------------------------------------------------------
# Lógica principal de carga
# ---------------------------------------------------------------------------

def _process_single_id(
    conn,
    session: requests.Session,
    item_id: int,
    summary: LoadSummary,
    is_retry: bool = False,
) -> bool:
    """Processa um único ID. Retorna True se bem-sucedido."""
    summary.consulted += 1
    item, error = fetch_item(session, item_id)

    if error:
        logger.warning(f"ID {item_id} falhou: {error}")
        add_failed(conn, item_id, error)
        summary.failures += 1
        return False

    if item is None:
        # Item deletado — conta como processado para não reter no watermark
        summary.ignored += 1
        logger.debug(f"ID {item_id}: null (item deletado)")
    else:
        action = upsert_item(conn, item)
        if action == "inserted":
            summary.inserted += 1
        else:
            summary.updated += 1
        logger.debug(f"ID {item_id}: {action} ({item.get('type', '?')})")

    if is_retry:
        remove_failed(conn, item_id)
        summary.retried_ok += 1

    return True


def run_load(
    initial_batch_size: int = DEFAULT_BATCH_SIZE,
    db_path: Path = DB_PATH,
) -> LoadSummary:
    """
    Executa um ciclo de carga completo (inicial ou incremental).
    É seguro chamar múltiplas vezes consecutivas — idempotente.
    """
    t_start = time.time()
    session = make_session()
    conn = get_connection(db_path)
    init_db(conn)

    # Determina faixa a processar
    max_id = fetch_max_item(session)
    last_id_str = get_state(conn, STATE_LAST_ID)

    if last_id_str is None:
        start_id = max(1, max_id - initial_batch_size + 1)
        logger.info(
            f"Primeira execução → carga inicial de {initial_batch_size} itens "
            f"(IDs {start_id}–{max_id})"
        )
    else:
        start_id = int(last_id_str) + 1
        logger.info(f"Execução incremental → IDs {start_id}–{max_id}")

    summary = LoadSummary(start_id=start_id, end_id=max_id)

    # ------------------------------------------------------------------
    # Fase 1: reprocessar IDs que falharam anteriormente
    # ------------------------------------------------------------------
    failed_ids = get_failed_ids(conn)
    if failed_ids:
        logger.info(f"Reprocessando {len(failed_ids)} IDs com falhas anteriores...")
        for fid in failed_ids:
            _process_single_id(conn, session, fid, summary, is_retry=True)

    # ------------------------------------------------------------------
    # Fase 2: processar IDs novos
    # ------------------------------------------------------------------
    if start_id > max_id:
        logger.info("Nenhum ID novo para processar.")
        summary.duration_seconds = time.time() - t_start
        return summary

    last_successful_id = int(last_id_str) if last_id_str else start_id - 1

    for item_id in range(start_id, max_id + 1):
        ok = _process_single_id(conn, session, item_id, summary)
        if ok:
            last_successful_id = item_id

    # Atualiza watermark até o maior ID processado com sucesso
    if last_successful_id >= start_id:
        set_state(conn, STATE_LAST_ID, str(last_successful_id))
        logger.info(f"Watermark atualizado para {last_successful_id}")

    summary.duration_seconds = time.time() - t_start
    conn.close()
    return summary


# ---------------------------------------------------------------------------
# Entrypoint CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hacker News Incremental Loader")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Itens na carga inicial (default: {DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()

    summary = run_load(initial_batch_size=args.batch_size)
    print(summary.report())


if __name__ == "__main__":
    main()
