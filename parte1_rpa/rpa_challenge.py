"""
Parte 1 — Automação RPA Challenge
==================================
Acessa https://rpachallenge.com/, faz download da planilha, e preenche
o formulário dinâmico para todos os registros com 100 % de acurácia.

Decisão de biblioteca: Playwright (async)
- Auto-wait nativo elimina sleeps frágeis.
- get_by_label() identifica campos pelo texto da label, independente
  da posição visual — critério principal do desafio.
- Suporte headless/não-headless via flag.
- API moderna e bem mantida.

Uso:
    python -m parte1_rpa.rpa_challenge              # headless
    python -m parte1_rpa.rpa_challenge --no-headless
"""

import argparse
import asyncio
import json
import logging
import time
from io import BytesIO
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, Page, Download

# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

RPA_URL = "https://rpachallenge.com/"

# Mapeamento: coluna do Excel → texto exato da label no formulário.
# Isolado aqui para facilitar manutenção caso o site mude labels.
COLUMN_TO_LABEL: dict[str, str] = {
    "First Name": "First Name",
    "Last Name": "Last Name",
    "Company Name": "Company Name",
    "Role in Company": "Role in Company",
    "Address": "Address",
    "Email": "Email",
    "Phone Number": "Phone Number",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def download_excel(page: Page) -> bytes:
    """Clica no botão de download e retorna o conteúdo do arquivo."""
    logger.info("Baixando planilha do desafio...")
    async with page.expect_download() as dl_info:
        await page.get_by_role("link", name="Download Excel").click()
    download: Download = await dl_info.value
    stream = await download.failure()
    if stream:
        raise RuntimeError(f"Download falhou: {stream}")
    path = await download.path()
    data = Path(path).read_bytes()
    logger.info(f"Planilha baixada ({len(data)} bytes)")
    return data


def parse_excel(data: bytes) -> list[dict]:
    """Lê a planilha e retorna lista de dicionários (uma por linha de dado)."""
    df = pd.read_excel(BytesIO(data), engine="openpyxl")
    # Remove colunas/linhas totalmente vazias que possam existir
    df.dropna(how="all", inplace=True)
    df.columns = [str(c).strip() for c in df.columns]
    records = df.to_dict(orient="records")
    logger.info(f"Planilha lida: {len(records)} registros, colunas: {list(df.columns)}")
    return records


async def fill_record(page: Page, record: dict) -> None:
    """
    Preenche todos os campos do formulário para um registro.

    Usa get_by_label() — localiza inputs pelo texto da label associada,
    não por posição ou seletor CSS frágil. Isso garante acurácia mesmo
    quando a ordem visual dos campos muda a cada submissão.
    """
    for col, label_text in COLUMN_TO_LABEL.items():
        value = record.get(col)
        if value is None:
            logger.warning(f"Coluna '{col}' ausente no registro; pulando.")
            continue
        field = page.get_by_label(label_text)
        await field.wait_for(state="visible", timeout=5_000)
        await field.fill(str(value).strip())
        logger.debug(f"  {label_text}: '{value}'")


async def run_challenge(headless: bool = True) -> dict:
    """
    Executa o RPA Challenge completo e retorna métricas de resultado.

    Returns:
        dict com campos: records_total, records_submitted, accuracy,
        duration_seconds, screenshot_path, result_text.
    """
    t_start = time.time()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # ------------------------------------------------------------------
        # 1. Abrir página e baixar planilha
        # ------------------------------------------------------------------
        logger.info(f"Navegando para {RPA_URL}")
        await page.goto(RPA_URL, wait_until="networkidle")

        excel_bytes = await download_excel(page)
        records = parse_excel(excel_bytes)

        # ------------------------------------------------------------------
        # 2. Iniciar desafio
        # ------------------------------------------------------------------
        logger.info("Clicando em Start...")
        await page.get_by_role("button", name="Start").click()
        await page.wait_for_load_state("networkidle")

        # ------------------------------------------------------------------
        # 3. Preencher formulário para cada registro
        # ------------------------------------------------------------------
        submitted = 0
        for i, record in enumerate(records, start=1):
            logger.info(f"Registro {i}/{len(records)}: {record.get('First Name')} {record.get('Last Name')}")
            await fill_record(page, record)

            submit_btn = page.get_by_role("button", name="Submit")
            await submit_btn.wait_for(state="visible", timeout=5_000)
            await submit_btn.click()
            submitted += 1
            logger.info(f"  Submetido com sucesso.")

        # ------------------------------------------------------------------
        # 4. Capturar resultado
        # ------------------------------------------------------------------
        await page.wait_for_timeout(1_000)  # aguarda renderização do resultado

        # Tenta ler o texto de resultado exibido na página
        try:
            result_locator = page.locator("div.congratulations, div.result, h2")
            result_text = await result_locator.first.inner_text(timeout=5_000)
        except Exception:
            result_text = await page.inner_text("body")

        screenshot_path = ARTIFACTS_DIR / "rpa_result.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info(f"Screenshot salvo em {screenshot_path}")

        await browser.close()

    duration = time.time() - t_start
    # Extrai acurácia do texto de resultado quando disponível
    accuracy = _parse_accuracy(result_text)

    result = {
        "records_total": len(records),
        "records_submitted": submitted,
        "accuracy": accuracy,
        "duration_seconds": round(duration, 2),
        "screenshot_path": str(screenshot_path),
        "result_text": result_text.strip(),
    }

    # Salva evidência em JSON
    evidence_path = ARTIFACTS_DIR / "rpa_evidence.json"
    evidence_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    logger.info(f"Evidência salva em {evidence_path}")

    return result


def _parse_accuracy(text: str) -> str:
    """Extrai o percentual de acurácia do texto de resultado, se disponível."""
    import re
    match = re.search(r"(\d+)\s*%", text)
    return f"{match.group(1)}%" if match else "ver screenshot"


# ---------------------------------------------------------------------------
# Entrypoint CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="RPA Challenge Automation")
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        default=True,
        help="Executa com browser visível (não headless)",
    )
    args = parser.parse_args()

    result = asyncio.run(run_challenge(headless=args.headless))

    print("\n=== RESULTADO ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
