# -*- coding: utf-8 -*-
"""
PERPETUA-PDF-FIX-01 — regressão da geração de PDF da Perpétua.

Prova, em processo e sem nenhuma dependência externa, que:
  1. Duas linhas não vazias consecutivas geram PDF (era o gatilho mínimo do
     bug "Not enough horizontal space to render a single character" — o
     multi_cell do fpdf2 deixava o cursor na borda direita da página).
  2. Parágrafos consecutivos, listas com bullets consecutivos e subtítulo
     seguido de várias linhas renderizam sem exceção.
  3. Conteúdos "difíceis" (CRLF, palavra de 800 chars, URL longa, e-mail,
     UUID, JSON em linha única, tabela Markdown como texto, acentos, emoji,
     vazio, 300 linhas) não quebram a ferramenta.
  4. Todo sucesso produz arquivo real, não vazio, com magic bytes %PDF-,
     dentro do diretório temporário isolado, e download_url estável.
  5. Falha interna NÃO vaza detalhe de biblioteca/stack/caminho ao usuário:
     o retorno é a mensagem amigável estável (o stack trace vai só ao log).

NÃO toca produção. Sem Gemini (GEMINI_API_KEY vazio de propósito), sem rede,
sem banco real (SQLite descartável em scratch/ apenas para satisfazer os
imports do módulo — nenhuma query é executada), sem dados reais. Os PDFs são
gravados em tempfile.TemporaryDirectory e removidos ao final.

Rodar:  python tests/test_perpetua_pdf_generation.py
   ou:  python -m pytest tests/test_perpetua_pdf_generation.py
"""
import json
import os
import pathlib
import sys
import tempfile

# raiz do repo no sys.path (permite `python tests/test_perpetua_pdf_generation.py`)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Ambiente DEVE ser definido ANTES de importar app.* (mesmo padrão da suíte
# test_perpetua_internal_auth.py). O banco nunca é consultado por estes testes.
pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/perpetua_pdf_test.db",
    # Estes testes nunca tocam o banco — desativar o seed evita exigir senha.
    "SEED_INITIAL_ADMIN": "false",
    # GEMINI vazio de propósito: nenhum teste pode chamar o Gemini de verdade.
    "GEMINI_API_KEY": "",
})

from app.services import ai_tools  # noqa: E402

FRIENDLY_ERROR = "Não foi possível gerar o PDF"


def _generate(content, title="Documento de Teste", filename="teste_pdf"):
    """Chama a ferramenta real com UPLOAD_DIR apontando para um tempdir isolado.

    Retorna (payload_dict, caminho_do_arquivo_ou_None, tmpdir_usado).
    O tempdir é criado por chamada e o arquivo é validado ANTES de o tempdir
    ser destruído pelo caller (validação acontece aqui dentro).
    """
    with tempfile.TemporaryDirectory(prefix="perpetua_pdf_test_") as tmpdir:
        old_upload_dir = ai_tools.UPLOAD_DIR
        ai_tools.UPLOAD_DIR = tmpdir
        try:
            raw = ai_tools.generate_pdf_document(filename=filename, title=title, content=content)
        finally:
            ai_tools.UPLOAD_DIR = old_upload_dir
        payload = json.loads(raw)

        if payload.get("success"):
            fname = payload["filename"]
            fpath = os.path.join(tmpdir, fname)
            # arquivo criado, dentro do tempdir, não vazio, com magic %PDF-
            assert os.path.isfile(fpath), f"arquivo não criado: {fpath}"
            assert os.path.commonpath([tmpdir, fpath]) == os.path.normpath(tmpdir), \
                "arquivo fora do diretório temporário esperado"
            size = os.path.getsize(fpath)
            assert size > 0, "arquivo vazio"
            with open(fpath, "rb") as f:
                magic = f.read(5)
            assert magic == b"%PDF-", f"magic bytes inválidos: {magic!r}"
            assert payload["download_url"] == f"/api/ai/download/{fname}", \
                "download_url mudou de formato"
            assert fname.endswith(".pdf")
        return payload


def _assert_success(content, **kw):
    payload = _generate(content, **kw)
    assert payload.get("success") is True, f"esperava sucesso, veio: {payload}"
    return payload


# ── 1. Regressão principal: gatilho mínimo do bug ──────────────────────────

def test_regressao_duas_linhas_consecutivas():
    """'linha um\\nlinha dois' — falhava antes da correção do cursor."""
    _assert_success("linha um\nlinha dois")


def test_tres_paragrafos_consecutivos():
    _assert_success("Primeiro parágrafo do documento.\n"
                    "Segundo parágrafo, imediatamente após o primeiro.\n"
                    "Terceiro parágrafo, também sem linha em branco.")


def test_lista_com_tres_bullets_consecutivos():
    _assert_success("- primeiro item\n- segundo item\n- terceiro item com texto "
                    "bem mais longo para forçar quebra de linha dentro da célula")


def test_subtitulo_seguido_de_varias_linhas():
    _assert_success("## Resumo Executivo\nlinha um do resumo\nlinha dois do resumo\n"
                    "## Detalhes\n- item a\n- item b\nparágrafo final")


# ── 2. Conteúdos difíceis (matriz da auditoria PERPETUA-PDF-AUDIT-01) ──────

def test_crlf():
    _assert_success("linha um\r\nlinha dois\r\nlinha três\r\n")


def test_palavra_muito_longa_sem_espacos():
    _assert_success("A" * 800)


def test_url_longa():
    url = "https://exemplo.com/relatorio?" + "&".join(f"param{i}=valor{i}" for i in range(40))
    _assert_success(f"Acesse o relatório completo:\n{url}\nFim do documento.")


def test_email_longo():
    _assert_success("Contato principal:\n"
                    "contato.muito.longo.de.exemplo@subdominio.empresa-exemplo.com.br\n"
                    "Responder em até 24h.")


def test_uuid():
    _assert_success("Identificador do registro:\n"
                    "550e8400-e29b-41d4-a716-446655440000\nLinha seguinte.")


def test_json_linha_unica():
    doc = '{"leads": [' + ",".join('{"nome":"Exemplo","status":"ativo"}' for _ in range(30)) + "]}"
    _assert_success("Payload de exemplo:\n" + doc)


def test_tabela_markdown_como_texto():
    tabela = ("| Nome | Email | Status | Cidade | Data | Valor |\n"
              "|---|---|---|---|---|---|\n" +
              "\n".join("| Exemplo | exemplo@teste.com | Ativo | Calama | 01/01/2026 | $1.000 |"
                        for _ in range(10)))
    _assert_success(tabela)


def test_acentos_portugues():
    _assert_success("Atenção: ação, coração, você, São Paulo.\n"
                    "Segunda linha com acentuação: já, até, então, aliás.")


def test_emoji_nao_quebra():
    # Comportamento atual preservado: latin-1 'replace' troca emoji por '?'.
    # O teste garante apenas que NÃO há crash e o PDF é válido.
    _assert_success("Relatório 📊 com emoji 🚀\nSegunda linha ✅ concluída")


def test_conteudo_vazio():
    _assert_success("")


def test_conteudo_com_varias_linhas():
    _assert_success("\n".join(f"Linha {i} de conteúdo repetido para volume." for i in range(300)))


# ── 3. Falha interna não vaza detalhe ao usuário ────────────────────────────

def test_erro_interno_devolve_mensagem_amigavel():
    """Força falha real (diretório de gravação inexistente) e valida que o
    retorno é a mensagem estável — sem nome de biblioteca, caminho ou stack."""
    old_upload_dir = ai_tools.UPLOAD_DIR
    ai_tools.UPLOAD_DIR = os.path.join(
        tempfile.gettempdir(), "perpetua_pdf_test_dir_inexistente", "sub"
    )
    try:
        raw = ai_tools.generate_pdf_document(
            filename="teste_pdf", title="T", content="linha um\nlinha dois"
        )
    finally:
        ai_tools.UPLOAD_DIR = old_upload_dir
    payload = json.loads(raw)
    assert "success" not in payload or not payload["success"]
    msg = payload.get("error", "")
    assert FRIENDLY_ERROR in msg, f"mensagem amigável ausente: {payload}"
    lowered = msg.lower()
    for forbidden in ("fpdf", "traceback", "errno", "\\", "/", "exception"):
        assert forbidden not in lowered, f"detalhe interno vazou no erro: {msg!r}"


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
