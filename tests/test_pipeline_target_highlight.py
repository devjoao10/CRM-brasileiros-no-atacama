# -*- coding: utf-8 -*-
"""
PIPE-HL-01 — destaque visual do card alvo do deep-link.

Regressao observada em producao: chegando por /pipeline?lead_id=<id> o drawer
certo abria e a tela ia ate a etapa certa, mas NENHUM card ficava destacado.

Causa raiz: a regra CSS do PR #25 era `.kanban-card.card-alvo`, e `.kanban-card`
nao existe em elemento nenhum — o card e `.lead-card`. A classe era aplicada
pelo JavaScript, mas nao pintava nada. Regra morta.

O guard central aqui e o test_seletor_do_css_casa_a_classe_real_do_card: ele
compara o seletor com a classe que renderLeadCard realmente emite. Um teste que
so verificasse "existe uma regra CSS" teria passado com o bug em producao.

LIMITACAO ASSUMIDA: o repo nao tem Playwright/Selenium/Cypress e este hotfix
nao justifica instalar um. Nao da para medir pixel, contraste real sob o
backdrop, nem se o scroll deixou o card na viewport. O que da para garantir de
forma deterministica e a cadeia estatica (seletor casa a classe, a classe e
aplicada, nada a remove) mais o comportamento do backend (o alvo volta na
resposta mesmo fora da primeira pagina). O restante foi verificado a olho e
esta registrado no relatorio do PR.

Rodar:  python tests/test_pipeline_target_highlight.py
   ou:  python -m pytest tests/test_pipeline_target_highlight.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/pipeline_highlight.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

HTML = pathlib.Path("templates/pipeline.html").read_text(encoding="utf-8")
CLASSE = "pipeline-target-card"

N_CARDS = 60      # > limit default (30): o alvo do fundo fica fora da 1a pagina
_C = {}


def _setup():
    if _C:
        return _C["client"], _C["ctx"]

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.pipeline import Funnel, FunnelEntry

    db_file = pathlib.Path("scratch/pipeline_highlight.db")
    if db_file.exists():
        try:
            db_file.unlink()
        except PermissionError:
            pass

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        from datetime import datetime, timedelta, timezone
        funnel = Funnel(nome="Funil HL", etapas=[{"id": "e1", "nome": "Etapa 1"}])
        db.add(funnel)
        db.add_all([Lead(nome=f"Lead {i:03d}", whatsapp=f"+5551{i:09d}")
                    for i in range(N_CARDS)])
        db.commit()
        db.refresh(funnel)
        leads = db.query(Lead).order_by(Lead.id).all()
        agora = datetime.now(timezone.utc)
        db.add_all([
            FunnelEntry(lead_id=l.id, funnel_id=funnel.id, etapa_id="e1",
                        posicao=i, updated_at=agora - timedelta(minutes=i))
            for i, l in enumerate(leads)
        ])
        db.commit()
        ctx = {"funnel_id": funnel.id,
               "topo": leads[0].id,        # 1o da pagina
               "fundo": leads[-1].id}      # ultimo: fora dos 30
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login: {r.status_code}"
    _C["client"], _C["ctx"] = client, ctx
    return client, ctx


# ── 7. o guard que teria pego o bug ──────────────────────────────────────

def test_seletor_do_css_casa_a_classe_real_do_card():
    """
    O bug de producao: regra em `.kanban-card`, card renderizado como
    `.lead-card`. Aqui a classe do card sai do PROPRIO renderLeadCard.
    """
    corpo = HTML.split("function renderLeadCard", 1)[1]
    m = re.search(r'<div class="([a-z0-9 _-]+)"[^>]*data-lead-id', corpo)
    assert m, "nao achei o div do card com data-lead-id em renderLeadCard"
    classes_do_card = set(m.group(1).split())

    regras = re.findall(r"\.([a-z0-9_-]+)\." + re.escape(CLASSE) + r"\s*\{", HTML)
    assert regras, f"nenhuma regra CSS para .{CLASSE}"
    for base in regras:
        assert base in classes_do_card, (
            f"regra CSS '.{base}.{CLASSE}' nunca casa: o card e "
            f"'{' '.join(sorted(classes_do_card))}'. Foi exatamente este o bug."
        )


def test_nao_sobrou_seletor_morto_de_kanban_card():
    assert 'class="kanban-card' not in HTML, "sanidade: .kanban-card nao e usado"
    # so REGRA CSS conta; o comentario que explica o bug pode citar a classe
    regras_mortas = re.findall(r"\.kanban-card[a-z0-9_.-]*\s*\{", HTML)
    assert not regras_mortas, (
        f"regra CSS orfa em .kanban-card (nenhum elemento tem a classe): {regras_mortas}"
    )


def test_css_do_destaque_e_forte_o_bastante():
    bloco = HTML.split(f".lead-card.{CLASSE}", 1)[1].split("}", 1)[0]
    assert "border" in bloco and "box-shadow" in bloco, (
        "o destaque precisa de borda E anel para atravessar o backdrop do drawer"
    )
    # o overlay e rgba(0,0,0,.4) + blur: um anel translucido demais some
    m = re.search(r"box-shadow:[^;]*rgba\([^)]*?,\s*\.?(\d*\.?\d+)\)", bloco)
    assert m and float(m.group(1)) >= 0.3, (
        f"anel fraco demais para ler sob o overlay do drawer: {bloco.strip()[:120]}"
    )


def test_animacao_e_curta_e_nao_infinita():
    bloco = HTML.split(f".lead-card.{CLASSE}", 1)[1].split("}", 1)[0]
    if "animation" in bloco:
        assert "infinite" not in bloco, "animacao continua foi proibida no escopo"
        m = re.search(r"animation:[^;]*?(\d*\.?\d+)s", bloco)
        assert m and float(m.group(1)) <= 1.5, f"animacao longa demais: {bloco}"
        assert "prefers-reduced-motion" in HTML, (
            "com animacao, respeitar prefers-reduced-motion"
        )


# ── 1, 2. deep-link identifica e marca o target ──────────────────────────

def test_deep_link_consulta_o_locate_e_marca_o_target():
    assert "/api/pipeline/locate/" in HTML, "deep-link precisa chamar /locate"
    assert f"classList.add('{CLASSE}')" in HTML, (
        f"o card alvo precisa receber a classe {CLASSE}"
    )
    assert "setTargetLead(lid)" in HTML, (
        "o target do deep-link precisa passar pela fonte unica de verdade"
    )


def test_card_expoe_data_lead_id_para_o_seletor_encontrar():
    corpo = HTML.split("function renderLeadCard", 1)[1]
    assert "data-lead-id=" in corpo, "sem data-lead-id o highlight nao acha o card"
    assert 'querySelector(\'[data-lead-id="\'' in HTML, (
        "o highlight precisa localizar o card pelo mesmo atributo que ele emite"
    )


# ── 3, 4. alvo fora dos 30, sem duplicar ─────────────────────────────────

def test_alvo_fora_da_primeira_pagina_volta_do_backend():
    client, ctx = _setup()
    url = (f"/api/pipeline/board/{ctx['funnel_id']}/stage/e1"
           f"?limit=30&include_lead_id={ctx['fundo']}")
    body = client.get(url).json()
    assert len(body["items"]) == 30, "include_lead_id nao pode inflar a pagina"
    assert body["target"] and body["target"]["lead_id"] == ctx["fundo"], (
        "o alvo distante precisa vir em `target` para poder ser destacado"
    )


def test_alvo_nao_e_duplicado_no_dom():
    """Uma FunnelEntry = um card. O target so entra se ainda nao estiver na lista."""
    client, ctx = _setup()
    url = (f"/api/pipeline/board/{ctx['funnel_id']}/stage/e1"
           f"?limit=30&include_lead_id={ctx['topo']}")
    body = client.get(url).json()
    assert body["target"] is None, "alvo ja na pagina nao pode voltar tambem em target"

    assert "!st.items.some(i => i.lead_id === data.target.lead_id)" in HTML, (
        "o frontend precisa checar antes de inserir o target na lista"
    )


# ── 5. fechar o drawer NAO remove o destaque ─────────────────────────────

def test_fechar_drawer_nao_apaga_o_highlight():
    fechar = HTML.split("function closeDetailPanel", 1)[1].split("\n    }", 1)[0]
    assert CLASSE not in fechar, (
        "closeDetailPanel nao pode remover a classe do target: o motivo do "
        "recurso e o atendente achar o lead DEPOIS de fechar as informacoes"
    )
    assert "highlightLeadId" not in fechar, (
        "closeDetailPanel nao pode limpar o target"
    )
    assert "setTargetLead" not in fechar, "closeDetailPanel nao pode resetar o target"


# ── 6. target A -> target B ──────────────────────────────────────────────

def test_novo_target_limpa_o_anterior():
    corpo = HTML.split("function setTargetLead", 1)[1].split("\n    }", 1)[0]
    assert f"querySelectorAll('.{CLASSE}')" in corpo, (
        "setar um novo target precisa varrer o DOM e limpar o anterior — "
        "a coluna antiga pode nao re-renderizar"
    )
    assert "classList.remove" in corpo, "o destaque anterior precisa ser removido"
    assert "highlightLeadId = leadId" in corpo, "o estado precisa acompanhar"


# ── scroll preservado ────────────────────────────────────────────────────

def test_scroll_leva_o_card_para_fora_do_drawer():
    assert "scrollIntoView" in HTML, "o scroll ate a etapa ja funcionava, preservar"
    m = re.search(r"scrollIntoView\(\{([^}]*)\}", HTML)
    assert m and "inline: 'center'" in m.group(1), (
        "o drawer cobre ~470px a direita; centralizar tambem na horizontal "
        "evita o card ficar embaixo dele"
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
        except Exception as exc:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if failures else 0)
