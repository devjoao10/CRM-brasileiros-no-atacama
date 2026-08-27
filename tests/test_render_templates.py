"""
Smoke test de renderização dos templates Jinja2 (WP-QA-01).

Valida que todos os templates migrados para base.html renderizam sem erro e
herdam o shell de layout. NÃO sobe o app nem toca banco — é puramente Jinja.

Rodar:  python -m pytest tests/test_render_templates.py
   ou:  python tests/test_render_templates.py
"""
import os
import jinja2

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

MIGRATED = [
    "dashboard.html", "tags.html", "relatorios.html", "segmentacao.html",
    "tarefas.html", "leads.html", "pipeline.html", "ai.html", "equipes.html",
    "operational/boards.html", "operational/pending.html", "operational/kanban.html",
    "gestao/pendencias.html",
]

CONTEXT = {"board_id": 1, "page_title": "Test", "active_nav": "dashboard",
           "user": {"nome": "Teste", "role": "admin"}}


def _env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR), autoescape=True
    )


def render(name: str) -> str:
    return _env().get_template(name).render(**CONTEXT)


# WP-OP-UI-02: paginas full-screen (sem sidebar; volta via botao proprio no header)
FULLSCREEN = {"operational/kanban.html"}


def test_all_templates_render():
    env = _env()
    for name in MIGRATED:
        html = env.get_template(name).render(**CONTEXT)
        assert "<!DOCTYPE html>" in html, f"{name}: sem DOCTYPE (base.html nao resolveu)"
        assert "{% " not in html, f"{name}: tag Jinja nao resolvida"
        assert 'class="top-header"' in html, f"{name}: topbar ausente"
        if name in FULLSCREEN:
            assert html.count('class="sidebar"') == 0, f"{name}: fullscreen nao deve ter sidebar"
            assert "Voltar para Quadros" in html, f"{name}: fullscreen sem botao de volta"
        else:
            assert 'class="sidebar"' in html, f"{name}: sidebar ausente"
            assert html.count('class="nav-item active"') == 1, f"{name}: item ativo != 1"
        assert "localhost:5678" not in html, f"{name}: link localhost:5678 reintroduzido"


def test_ai_chat_is_sanitized():
    """SEC-XSS-01: render do chat da IA deve passar por DOMPurify."""
    html = render("ai.html")
    assert "dompurify" in html.lower(), "ai.html: DOMPurify nao carregado"
    assert "DOMPurify.sanitize(marked.parse(content))" in html, "ai.html: IA sem sanitize"


# WP-UX-03 — sidebars contextuais: cada pagina renderiza SO a navegacao do seu setor.
SECTOR_OF = {
    "dashboard.html": "comercial", "ai.html": "comercial", "leads.html": "comercial",
    "tags.html": "comercial", "pipeline.html": "comercial", "segmentacao.html": "comercial",
    "tarefas.html": "comercial", "relatorios.html": "comercial",
    "operational/boards.html": "operacional", "operational/kanban.html": "operacional",
    "operational/pending.html": "gestao", "equipes.html": "gestao",
    "gestao/pendencias.html": "gestao",
}
FORBIDDEN_LINKS = {
    # links de sidebar que NAO podem aparecer fora do proprio setor
    "comercial": ['href="/operational/boards"', 'href="/operational/my-pending"', 'href="/equipe"'],
    "operacional": ['href="/leads"', 'href="/pipeline"', 'href="/tags"'],
    "gestao": ['href="/leads"', 'href="/pipeline"', 'href="/tags"'],
}


def test_sector_sidebar_isolation():
    env = _env()
    for name, sector in SECTOR_OF.items():
        html = env.get_template(name).render(**CONTEXT)
        if name not in FULLSCREEN:
            assert 'href="/hub"' in html, f"{name}: link de volta ao Hub ausente"
        for bad in FORBIDDEN_LINKS[sector]:
            assert bad not in html, f"{name} ({sector}): vazou link de outro setor: {bad}"


def test_listas_que_trocam_de_filtro_sequenciam_a_requisicao():
    """
    AUDIT-2026-08-WG (F-442) — resposta obsoleta pintando a tela.

    Toda lista que troca de filtro/aba sem cancelar a chamada anterior pode
    receber a resposta ANTIGA depois da nova e pintar dados do filtro errado,
    sem nenhum erro visivel. Aconteceu em tres telas independentes; a defesa e
    sempre a mesma — um contador de sequencia comparado DEPOIS do await.

    Este teste percorre as telas onde isso ja mordeu. Uma tela nova com o mesmo
    padrao nao cai aqui sozinha (o teste nao adivinha intencao), mas o custo de
    acrescentar uma linha e baixo e o defeito e recorrente.
    """
    import pathlib as _p

    raiz = _p.Path(__file__).resolve().parent.parent
    alvos = {
        "templates/tarefas.html": "tasksRequestSeq",
        "templates/segmentacao.html": "previewRequestSeq",
        "conversas/static/js/conversas.js": "listRequestSeq",
    }
    for caminho, contador in alvos.items():
        fonte = (raiz / caminho).read_text(encoding="utf-8")
        assert contador in fonte, f"{caminho}: contador de sequencia `{contador}` sumiu"
        assert f"++{contador}" in fonte,             f"{caminho}: `{contador}` nunca e incrementado — o guard vira no-op"
        assert f"!== {contador}" in fonte or f"!= {contador}" in fonte,             f"{caminho}: `{contador}` e incrementado mas nunca comparado depois do await"


if __name__ == "__main__":
    test_all_templates_render()
    test_listas_que_trocam_de_filtro_sequenciam_a_requisicao()
    test_ai_chat_is_sanitized()
    test_sector_sidebar_isolation()
    print("OK: todos os render smoke tests passaram")
