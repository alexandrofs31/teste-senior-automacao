"""
Testes unitários do mapeamento RPA (sem browser).
Valida: parse do Excel, mapeamento coluna→label, normalização de strings.
"""

from io import BytesIO

import pandas as pd
import pytest

from parte1_rpa.rpa_challenge import COLUMN_TO_LABEL, parse_excel, _parse_accuracy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_excel_bytes(rows: list[dict]) -> bytes:
    """Gera bytes de um arquivo Excel a partir de uma lista de dicts."""
    df = pd.DataFrame(rows)
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Testes de parse do Excel
# ---------------------------------------------------------------------------

SAMPLE_ROWS = [
    {
        "First Name": "John",
        "Last Name": "Doe",
        "Company Name": "Acme",
        "Role in Company": "Engineer",
        "Address": "123 Main St",
        "Email": "john@acme.com",
        "Phone Number": "555-1234",
    },
    {
        "First Name": "Jane",
        "Last Name": "Smith",
        "Company Name": "Corp",
        "Role in Company": "Manager",
        "Address": "456 Oak Ave",
        "Email": "jane@corp.com",
        "Phone Number": "555-5678",
    },
]


def test_parse_excel_retorna_lista_de_dicts():
    data = make_excel_bytes(SAMPLE_ROWS)
    records = parse_excel(data)
    assert isinstance(records, list)
    assert len(records) == 2


def test_parse_excel_preserva_valores():
    data = make_excel_bytes(SAMPLE_ROWS)
    records = parse_excel(data)
    assert records[0]["First Name"] == "John"
    assert records[1]["Email"] == "jane@corp.com"


def test_parse_excel_normaliza_nomes_de_colunas():
    """Colunas com espaços extras devem ser normalizadas."""
    rows = [{"  First Name  ": "Bob"}]
    df = pd.DataFrame(rows)
    buf = BytesIO()
    df.to_excel(buf, index=False)
    records = parse_excel(buf.getvalue())
    # Após strip, coluna deve ser "First Name"
    assert "First Name" in records[0]


def test_parse_excel_ignora_linhas_vazias():
    rows = SAMPLE_ROWS + [{}]  # linha vazia no final
    data = make_excel_bytes(rows)
    records = parse_excel(data)
    # Deve ter dropado a linha vazia
    assert all(r.get("First Name") is not None for r in records)


# ---------------------------------------------------------------------------
# Testes do mapeamento coluna → label
# ---------------------------------------------------------------------------

def test_mapeamento_cobre_todas_colunas_do_excel():
    """Todas as colunas esperadas no Excel devem ter mapeamento para label."""
    expected_columns = {
        "First Name", "Last Name", "Company Name",
        "Role in Company", "Address", "Email", "Phone Number",
    }
    assert set(COLUMN_TO_LABEL.keys()) == expected_columns


def test_mapeamento_nao_tem_valores_vazios():
    for col, label in COLUMN_TO_LABEL.items():
        assert label.strip(), f"Label vazia para coluna '{col}'"


# ---------------------------------------------------------------------------
# Testes do parser de acurácia
# ---------------------------------------------------------------------------

def test_parse_accuracy_extrai_percentual():
    assert _parse_accuracy("Accuracy: 100%") == "100%"
    assert _parse_accuracy("You scored 85% !") == "85%"


def test_parse_accuracy_fallback():
    assert _parse_accuracy("Parabéns!") == "ver screenshot"
