"""
WP-OP-UI — Board operacional full-screen + botões condicionais.

Prova, no HTML renderizado do kanban:
- OP-UI-02: sem sidebar (full-screen) + margin-left:0 + botão "Voltar para Quadros";
- OP-UI-04: contrato dos botões condicionais (mensagem obrigatória, função de
  estado, guard de clique, empty-state) presente no código entregue ao browser;
- OP-UI-03: chip de prazo com estado 'overdue'.

Rodar:  python tests/test_kanban_ui.py   ou   python -m pytest tests/test_kanban_ui.py
"""
import pathlib
import sys

import jinja2

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NO_LISTS_MSG = "Crie uma coluna antes de criar um card."


def _render() -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True
    )
    return env.get_template("operational/kanban.html").render(
        board_id=1, user={"nome": "Teste", "role": "admin"}
    )


def test_kanban_fullscreen():
    html = _render()
    assert html.count('class="sidebar"') == 0, "kanban full-screen nao deve ter sidebar"
    assert "margin-left: 0" in html, "main-content deve ocupar toda a largura"
    assert "Voltar para Quadros" in html, "botao de volta obrigatorio no header do board"
    assert 'id="newCardBtn"' in html and 'id="newListBtn"' in html


def test_kanban_conditional_buttons_contract():
    html = _render()
    # mensagem obrigatoria do SDD (secao 7.6)
    assert NO_LISTS_MSG in html, "mensagem obrigatoria ausente"
    # funcao de estado + guard de clique + empty-state
    assert "updateCreateCardState" in html, "funcao de estado do botao ausente"
    assert "btn.disabled = empty" in html, "botao nao e desabilitado sem colunas"
    assert "board-empty" in html, "empty-state de quadro sem colunas ausente"
    # select de colunas escapa o nome (XSS)
    assert "esc(l.name)" in html, "nome da coluna sem escape no select"


def test_kanban_due_chip():
    html = _render()
    assert "kanban-due" in html and "overdue" in html, "chip de prazo/atraso ausente"


def test_kanban_trello_ui():
    """Redesign Trello + fix dos modais (regressao do smoke visual)."""
    html = _render()
    # fix critico: modais como overlay (CSS de .modal se perdeu na migracao)
    assert ".modal.show { display: flex; }" in html, "modal sem CSS de overlay (bug do smoke)"
    assert "position: fixed; inset: 0;" in html, "modal nao cobre a tela"
    # §7.2: botao de adicionar card NA coluna + pre-selecao da coluna
    assert "kanban-add-card" in html and "Adicionar um card" in html, "botao por coluna ausente"
    assert "openCreateCardModal" in html and "select.value = String(preListId)" in html
    # coluna-fantasma para criar listas (admin)
    assert "kanban-add-list" in html and "Adicionar outra lista" in html, "coluna-fantasma ausente"
    # visual de arraste
    assert 'classList.add("dragging")' in html, "feedback visual de drag ausente"


if __name__ == "__main__":
    test_kanban_fullscreen()
    test_kanban_conditional_buttons_contract()
    test_kanban_due_chip()
    test_kanban_trello_ui()
    print("OK: kanban full-screen + botoes condicionais + prazo + trello-ui/modais")
