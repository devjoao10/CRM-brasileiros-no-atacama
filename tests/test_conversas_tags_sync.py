"""
CONV-TAGS-SYNC-01 — Sync de tags do lead (CRM) <-> Conversas.

O sync usa o padrao SQL-direto na base COMPARTILHADA (mesmo padrao do
crm.py). Este teste cria as tabelas do CRM (tags/lead_tags) NO MESMO
sqlite e prova o sync REAL, sem mock.

Prova que:
  0. DEV ISOLADO (antes de existirem as tabelas CRM): abrir conversa e
     aplicar tag funcionam 100% local, sem excecao.
  1. CRM -> Conversas: abrir conversa vinculada espelha as tags do lead
     (tag local criada por NOME, cor copiada).
  2. Espelho EXATO: tag removida do lead no CRM some da conversa no
     proximo GET; tag nova no CRM aparece.
  3. Conversas -> CRM: aplicar tag em conversa vinculada grava em
     lead_tags (criando a tag no CRM por nome se faltar); remover apaga o
     vinculo. Idempotente.
  4. Conversa SEM lead: aplicar/remover NAO toca as tabelas do CRM.
  5. Cor invalida vinda do CRM e SANITIZADA (vai para style attr).
  6. Guard do CONV-BF-UI-02: .conv-filters com flex-wrap (abas visiveis).

Roda standalone:  python tests/test_conversas_tags_sync.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_tags_sync_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)

LEAD_ID = 42
s = SessionLocal()
conv_linked = Conversation(lead_id=LEAD_ID, whatsapp="5511900099991", nome="Lead Vinculado", status="aberta")
conv_free = Conversation(lead_id=0, whatsapp="5511900099992", nome="Sem Lead", status="aberta")
s.add_all([conv_linked, conv_free])
s.commit()
s.refresh(conv_linked)
s.refresh(conv_free)
CID_L, CID_F = conv_linked.id, conv_free.id
s.close()


def crm_exec(sql, params=None):
    s = SessionLocal()
    try:
        result = s.execute(text(sql), params or {})
        s.commit()
        try:
            return result.fetchall()
        except Exception:
            return None
    finally:
        s.close()


# ============ 0. DEV ISOLADO (SEM tabelas do CRM) ============
print("SYNC — dev isolado (tabelas CRM ausentes) nao quebra")
r0 = client.get(f"/api/conversations/{CID_L}")
check(r0.status_code == 200, "abrir conversa vinculada sem CRM -> 200 (espelho no-op)")
r0b = client.post("/api/tags", json={"nome": "LocalOnly", "cor": "#111111"})
TAG_LOCAL = r0b.json()["id"]
r0c = client.post(f"/api/conversations/{CID_L}/tags/{TAG_LOCAL}")
check(r0c.status_code == 200 and len(r0c.json()) == 1,
      "aplicar tag sem CRM funciona local (replicacao so loga)")

# ============ cria as tabelas do CRM no MESMO banco ============
crm_exec("CREATE TABLE tags (id INTEGER PRIMARY KEY, nome VARCHAR(100) UNIQUE NOT NULL, "
         "cor VARCHAR(7) NOT NULL DEFAULT '#2B6CB0', created_at TIMESTAMP)")
crm_exec("CREATE TABLE lead_tags (lead_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, "
         "PRIMARY KEY (lead_id, tag_id))")

# lead 42 tem a tag VIP no CRM (cor valida) e a tag Sujo (cor INVALIDA)
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('VIP', '#EF4444', CURRENT_TIMESTAMP)")
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('Sujo', 'red;}</style>', CURRENT_TIMESTAMP)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 1)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 2)")


# ============ 1. CRM -> CONVERSAS (espelho ao abrir) ============
print("\nSYNC — CRM -> Conversas ao abrir a conversa")
r1 = client.get(f"/api/conversations/{CID_L}")
tags1 = {t["nome"]: t["cor"] for t in r1.json()["tags"]}
check("VIP" in tags1, "tag do lead no CRM aparece na conversa")
check(tags1.get("VIP") == "#EF4444", "cor copiada do CRM")
check("Sujo" in tags1 and tags1["Sujo"] == "#3B82F6",
      "cor INVALIDA do CRM sanitizada para default (nunca chega ao style)")
check("LocalOnly" not in tags1,
      "espelho EXATO: tag local previa (nao presente no lead) foi substituida pelo conjunto do CRM")

# idempotencia do espelho
r1b = client.get(f"/api/conversations/{CID_L}")
check(len(r1b.json()["tags"]) == 2, "2o GET nao duplica tags")


# ============ 2. ESPELHO EXATO (mudancas no CRM refletem) ============
print("\nSYNC — mudancas no CRM refletem no proximo GET")
crm_exec(f"DELETE FROM lead_tags WHERE lead_id = {LEAD_ID} AND tag_id = 2")  # remove Sujo
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('Novo', '#22C55E', CURRENT_TIMESTAMP)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 3)")
r2 = client.get(f"/api/conversations/{CID_L}")
nomes2 = {t["nome"] for t in r2.json()["tags"]}
check(nomes2 == {"VIP", "Novo"}, f"conjunto espelhado exato (got {nomes2})")


# ============ 3. CONVERSAS -> CRM (aplicar/remover replica) ============
print("\nSYNC — Conversas -> CRM")
r3 = client.post("/api/tags", json={"nome": "Orcamento", "cor": "#F59E0B"})
TAG_ORC = r3.json()["id"]
client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows = crm_exec(
    "SELECT t.nome FROM tags t JOIN lead_tags lt ON lt.tag_id = t.id "
    f"WHERE lt.lead_id = {LEAD_ID} AND t.nome = 'Orcamento'")
check(len(rows) == 1, "aplicar no Conversas criou a tag no CRM por nome e vinculou ao lead")

# aplicar 2x = idempotente nos dois lados
client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows_dup = crm_exec(f"SELECT COUNT(*) AS c FROM lead_tags WHERE lead_id = {LEAD_ID}")
check(rows_dup[0].c == 3, "aplicar 2x nao duplica vinculo no CRM")

# remover replica
client.delete(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows_after = crm_exec(
    "SELECT t.nome FROM tags t JOIN lead_tags lt ON lt.tag_id = t.id "
    f"WHERE lt.lead_id = {LEAD_ID} AND t.nome = 'Orcamento'")
check(len(rows_after) == 0, "remover no Conversas removeu o vinculo no CRM")


# ==== 3b. AUDIT-2026-08-WD — recusar remocao SO quando o espelho ressuscita ====
print()
print("SYNC — falha no CRM: recusa quando o espelho volta, permite quando nao volta")
# A remocao local e recusada (502) porque `sync_lead_tags_to_conversation`
# reespelha o conjunto do CRM a cada abertura: sem a remocao la, a tag volta
# sozinha. Mas esse espelho DESISTE quando o CRM esta inacessivel — e ai a
# remocao local nao volta, e recusar quebraria o inbox por um risco inexistente
# (foi o que aconteceu em dev isolado, onde as tabelas do CRM nem existem).
from app.routers import tags as tags_router  # noqa: E402

_orig_remove = tags_router.crm_service.remove_tag_from_lead
_orig_get = tags_router.crm_service.get_lead_tags

# reaplica a tag para ter o que remover nos dois cenarios
client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")

try:
    # (a) CRM ALCANCAVEL e a remocao falhou -> 502, e a tag CONTINUA local
    tags_router.crm_service.remove_tag_from_lead = lambda *a, **k: False
    tags_router.crm_service.get_lead_tags = lambda *a, **k: []      # alcancavel
    r_502 = client.delete(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
    check(r_502.status_code == 502,
          f"CRM alcancavel + remocao falhou -> 502 (obteve {r_502.status_code})")
    # Le o vinculo LOCAL direto no banco: `GET /api/conversations/{id}` abre a
    # conversa e dispara o espelho, que com o mock `get_lead_tags -> []`
    # esvaziaria as tags — e o teste estaria medindo o espelho, nao a rota.
    ainda = client.get(f"/api/conversations/{CID_L}?opening=false").json()["tags"]
    check(any(t["nome"] == "Orcamento" for t in ainda),
          "a tag NAO foi removida localmente — nao vai reaparecer no proximo reabrir")

    # (b) CRM INACESSIVEL -> 200, remove local (o espelho tambem nao roda)
    tags_router.crm_service.get_lead_tags = lambda *a, **k: None    # inacessivel
    r_ok = client.delete(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
    check(r_ok.status_code == 200,
          f"CRM inacessivel -> remocao local permitida (obteve {r_ok.status_code})")
    depois = client.get(f"/api/conversations/{CID_L}").json()["tags"]
    check(not any(t["nome"] == "Orcamento" for t in depois),
          "a tag saiu localmente mesmo com o CRM fora — o inbox nao trava")
finally:
    tags_router.crm_service.remove_tag_from_lead = _orig_remove
    tags_router.crm_service.get_lead_tags = _orig_get


# ==== 3c. AUDIT-2026-08-WF2 — simetrico para apply_tag ====
print()
print("SYNC — apply_tag: recusa quando o espelho apagaria, permite quando nao apaga")
# Mesmo raciocinio do bloco 3b, na direcao oposta: sync_lead_tags_to_conversation
# reespelha o CRM a cada abertura, entao aplicar SO localmente (sem propagar ao
# CRM) faz a tag sumir sozinha no proximo reabrir. TAG_ORC saiu de conv_linked
# no cenario (b) do bloco anterior — ponto de partida limpo para testar a
# aplicacao.
_orig_add = tags_router.crm_service.add_tag_to_lead
_orig_get2 = tags_router.crm_service.get_lead_tags

try:
    # (a) CRM ALCANCAVEL e a aplicacao falhou -> 502, tag NAO fica local
    tags_router.crm_service.add_tag_to_lead = lambda *a, **k: False
    tags_router.crm_service.get_lead_tags = lambda *a, **k: []      # alcancavel
    r_502b = client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
    check(r_502b.status_code == 502,
          f"CRM alcancavel + aplicacao falhou -> 502 (obteve {r_502b.status_code})")
    # ?opening=false pelo mesmo motivo do bloco 3b: um GET normal aqui dispara
    # o espelho, que com get_lead_tags -> [] esvaziaria TODAS as tags locais
    # (nao so a Orcamento) e o teste mediria o espelho, nao a rota.
    ainda2 = client.get(f"/api/conversations/{CID_L}?opening=false").json()["tags"]
    check(not any(t["nome"] == "Orcamento" for t in ainda2),
          "a tag NAO foi aplicada localmente — nao ia sumir sozinha no proximo reabrir")

    # (b) CRM INACESSIVEL -> 200, aplica local (o espelho tambem nao roda)
    tags_router.crm_service.get_lead_tags = lambda *a, **k: None    # inacessivel
    r_okb = client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
    check(r_okb.status_code == 200,
          f"CRM inacessivel -> aplicacao local permitida (obteve {r_okb.status_code})")
    depois2 = client.get(f"/api/conversations/{CID_L}").json()["tags"]
    check(any(t["nome"] == "Orcamento" for t in depois2),
          "a tag entrou localmente mesmo com o CRM fora — o inbox nao trava")
finally:
    tags_router.crm_service.add_tag_to_lead = _orig_add
    tags_router.crm_service.get_lead_tags = _orig_get2


# ============ 4. CONVERSA SEM LEAD = LOCAL PURO ============
print("\nSYNC — conversa sem lead nao toca o CRM")
crm_count_before = crm_exec("SELECT COUNT(*) AS c FROM lead_tags")[0].c
client.post(f"/api/conversations/{CID_F}/tags/{TAG_ORC}")
r4 = client.get(f"/api/conversations/{CID_F}")
check(any(t["nome"] == "Orcamento" for t in r4.json()["tags"]),
      "tag aplicada localmente na conversa sem lead")
crm_count_after = crm_exec("SELECT COUNT(*) AS c FROM lead_tags")[0].c
check(crm_count_after == crm_count_before, "lead_tags do CRM INTOCADO para conversa sem lead")


# ============ 5b. CONV-TAGS-UX-01: MODAL DE TAGS ============
print("\nCONV-TAGS-UX-01 — guard do modal de tags")
js_m = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html_m = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
css_m = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")
check("prompt(" not in js_m, "NENHUM prompt() nativo restante no JS")
check('id="tagModalOverlay"' in html_m and 'id="btnTagModalCreate"' in html_m
      and 'id="tagModalName"' in html_m, "markup do modal presente")
check("não está vinculada a um lead" in html_m,
      "aviso explicito de conversa NAO vinculada (nao finge sync)")
check("openTagModal" in js_m and "closeTagModal" in js_m and "renderTagModal" in js_m
      and "createAndApplyTagFromModal" in js_m, "funcoes do modal presentes")
check("window._modalApplyTag(${Number(t.id)}" in js_m
      and "window._modalRemoveTag(${Number(t.id)}" in js_m,
      "apply/remove do modal com ids numericos coercidos")
check("escapeHtml(t.nome)" in js_m and "safeTagColor" in js_m,
      "chips do modal com nome escapado e cor sanitizada")
check("resp.status === 409" in js_m, "409 (nome duplicado) reusa a tag existente (sem duplicar)")
check(".tag-modal-overlay" in css_m and ".tag-modal" in css_m, "CSS do modal presente")
check("lead_id) > 0" in js_m, "modal distingue conversa vinculada vs nao vinculada")

# ============ 6. GUARD DO BUGFIX DE UI ============
print("\nCONV-BF-UI-04 — guard estatico das abas (seletor dedicado + cache-bust)")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")
js_ui = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html_ui = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")

# A raiz do bug do UI-03: .conv-filters era compartilhada entre a linha de
# abas E o bloco de selects. Agora cada area tem classe DEDICADA.
check('class="conv-inbox"' in html_ui, "html: seletor de inbox usa .conv-inbox (classe dedicada)")
check('class="conv-sidebar-filter-selects"' in html_ui,
      "html: bloco de selects usa .conv-sidebar-filter-selects")
check('class="conv-filters"' not in html_ui,
      "html: classe ambigua .conv-filters ELIMINADA")
# PACOTE-B: a linha de abas rolavel foi substituida pelo seletor de inbox
# (<details> nativo). A INTENCAO do guard (classes dedicadas, sem seletor
# ambiguo, sem colisao com o bloco de selects) segue valendo no novo markup.
check(".conv-inbox {" in css and ".conv-inbox-menu {" in css,
      "css: seletor de inbox com classes dedicadas")
check("flex: 0 0 auto" in css, "css: badge/caret nao encolhem (flex 0 0 auto)")
check(".conv-inbox-menu button[data-inbox]" in js_ui,
      "js: clique das categorias mira .conv-inbox-menu")
check(".conv-filters button" not in js_ui, "js: NENHUM seletor antigo .conv-filters restante")
check(".conv-filter-tabs" not in js_ui and ".conv-filter-tabs" not in html_ui,
      "js/html: abas antigas ELIMINADAS (um unico sistema de categoria)")
check("conversas.css?v=" in html_ui and "conversas.js?v=" in html_ui,
      "html: referencias de CSS e JS com cache-bust")
for page in ("settings.html", "templates.html"):
    other = (CONVERSAS_DIR / "templates" / page).read_text(encoding="utf-8")
    check("conversas.css?v=" in other, f"{page}: CSS compartilhado tambem cache-busted")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE TAG SYNC PASSARAM")
