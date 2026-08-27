# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WC — regressao de C1 (tags: full-replace vs delta) e C3
(anotacoes: notas concorrentes + timestamp UTC-aware).

C1: PUT /api/tags/lead/{id} era SUBSTITUICAO TOTAL (`lead.tags = tags`) a
partir de `tag_ids`. O editor de lead no CRM monta essa lista a partir de um
snapshot tirado quando o modal ABRIU; qualquer tag aplicada/removida por outra
origem enquanto o modal estava aberto era apagada em silencio no save
("adicionei uma tag e as outras sumiram"). Este arquivo prova que o modo
full-replace continua identico ao corpo real que a `Tool Definir Tags Lead` do
n8n manda (nao pode quebrar), que o novo modo incremental (adicionar/remover)
funciona, que misturar os dois modos e 422, e que uma mudanca concorrente
entre o snapshot do cliente e o save NAO se perde em modo delta — o guard de
regressao do bug principal.

C3: append_anotacao fazia read-modify-write sem lock. Aqui so a parte que da
para provar em SQLite (duas notas sequenciais sobrevivem e ficam ordenadas, e
o timestamp passou a ser UTC-aware). A prova de concorrencia de verdade
(FOR UPDATE contra PostgreSQL real, com duas threads/duas conexoes) e um
script avulso em scratch/, fora desta suite (SQLite nao mostra a corrida).

Rodar:  python tests/test_tags_delta.py
"""
import io
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "tags_delta_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

sys.path.insert(0, str(ROOT))
import os  # noqa: E402
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.tag import Tag  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.user import UserRole  # noqa: E402

Base.metadata.create_all(bind=engine)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


class _Usuario:
    id = 1
    email = "n8n@local"
    nome = "Integracao n8n"
    role = UserRole.ADMIN
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _Usuario()
client = TestClient(main.app)


def _tag(db, nome, cor="#111111"):
    t = Tag(nome=nome, cor=cor)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _lead(db, nome="Lead Teste", tag_ids=None):
    """Cria um lead e, se pedido, associa tags RE-CONSULTADAS nesta sessao
    (evita reusar objeto ORM de uma sessao ja fechada)."""
    lead = Lead(nome=nome)
    db.add(lead)
    db.commit()
    if tag_ids:
        lead.tags = db.query(Tag).filter(Tag.id.in_(tag_ids)).all()
        db.commit()
    db.refresh(lead)
    return lead


def _tags_do_lead(lead_id):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        return {t.id for t in lead.tags}
    finally:
        db.close()


db = SessionLocal()
try:
    t1 = _tag(db, "T1")
    t2 = _tag(db, "T2")
    t3 = _tag(db, "T3")
    t4 = _tag(db, "T4")
    CTX = {"t1": t1.id, "t2": t2.id, "t3": t3.id, "t4": t4.id}
finally:
    db.close()


# ─── 0. o corpo full-replace testado vem do EXPORT, nao da memoria ───────
print("0) o corpo full-replace testado e o que a Tool Definir Tags Lead manda de verdade")

EXPORT = ROOT / "n8n" / "workflows" / "live_exports" / "20260826_wa" / "gerenciador_leads.json"
check(EXPORT.exists(), f"export versionado em {EXPORT.relative_to(ROOT).as_posix()}")

wf = json.load(io.open(EXPORT, encoding="utf-8"))
node = next((n for n in wf["nodes"] if n["name"] == "Tool Definir Tags Lead"), None)
check(node is not None, "node 'Tool Definir Tags Lead' presente no export do Gerenciador")

template = node["parameters"]["jsonBody"].lstrip("=") if node else ""
check(re.sub(r"\s+", "", template) == '{"tag_ids":{tag_ids}}',
      f"o template do n8n so tem a chave tag_ids, sem adicionar/remover (veio {template!r})")


# ─── 1. full-replace: corpo EXATO do n8n continua substituindo tudo ──────
print()
print("1) modo full-replace (tag_ids) continua funcionando com o corpo real do n8n")

db = SessionLocal()
try:
    lead1 = _lead(db, "Lead Full Replace", tag_ids=[CTX["t1"], CTX["t3"]])
finally:
    db.close()

corpo_n8n = json.loads(template.replace("{tag_ids}", json.dumps([CTX["t2"]])))
check(corpo_n8n == {"tag_ids": [CTX["t2"]]},
      f"corpo reconstruido do template bate com {{'tag_ids': [t2]}} (veio {corpo_n8n})")

r = client.put(f"/api/tags/lead/{lead1.id}", json=corpo_n8n)
check(r.status_code == 200, f"200 no full-replace (veio {r.status_code}: {r.text[:200]})")
check(_tags_do_lead(lead1.id) == {CTX["t2"]},
      f"tag_ids=[t2] SUBSTITUI t1/t3 por so t2 (tem {_tags_do_lead(lead1.id)})")


# ─── 2. adicionar: soma sem tocar nas demais ─────────────────────────────
print()
print("2) modo incremental: 'adicionar' inclui sem remover as demais")

db = SessionLocal()
try:
    lead2 = _lead(db, "Lead Adicionar", tag_ids=[CTX["t1"]])
finally:
    db.close()

r = client.put(f"/api/tags/lead/{lead2.id}", json={"adicionar": [CTX["t2"]]})
check(r.status_code == 200, f"200 ao adicionar (veio {r.status_code}: {r.text[:200]})")
check(_tags_do_lead(lead2.id) == {CTX["t1"], CTX["t2"]},
      f"t1 continua e t2 foi somada (tem {_tags_do_lead(lead2.id)})")


# ─── 3. remover: tira so o(s) id(s) informado(s) ─────────────────────────
print()
print("3) modo incremental: 'remover' tira so a tag informada")

r = client.put(f"/api/tags/lead/{lead2.id}", json={"remover": [CTX["t1"]]})
check(r.status_code == 200, f"200 ao remover (veio {r.status_code}: {r.text[:200]})")
check(_tags_do_lead(lead2.id) == {CTX["t2"]},
      f"so t1 saiu, t2 continua (tem {_tags_do_lead(lead2.id)})")


# ─── 4. misturar tag_ids com adicionar/remover e 422 ─────────────────────
print()
print("4) misturar full-replace com incremental na mesma chamada e rejeitado (422)")

for corpo in (
    {"tag_ids": [CTX["t1"]], "adicionar": [CTX["t2"]]},
    {"tag_ids": [CTX["t1"]], "remover": [CTX["t2"]]},
    {"tag_ids": [], "adicionar": []},
    {},  # nem tag_ids nem adicionar/remover: intencao ambigua
):
    r = client.put(f"/api/tags/lead/{lead2.id}", json=corpo)
    check(r.status_code == 422, f"422 para {corpo} (veio {r.status_code})")


# ─── 5. 404 quando o id em adicionar/remover nao existe ──────────────────
print()
print("5) adicionar/remover com tag inexistente e 404, sem alterar nada")

antes = _tags_do_lead(lead2.id)
r = client.put(f"/api/tags/lead/{lead2.id}", json={"adicionar": [999999]})
check(r.status_code == 404, f"404 para tag inexistente (veio {r.status_code})")
check(_tags_do_lead(lead2.id) == antes, "um 404 em adicionar nao altera as tags existentes")


# ─── 6. GUARDA DE REGRESSAO: mudanca concorrente NAO se perde em modo delta
print()
print("6) [REGRESSAO] mudanca concorrente entre snapshot e save sobrevive em modo delta")

db = SessionLocal()
try:
    lead3 = _lead(db, "Lead Concorrencia", tag_ids=[CTX["t1"], CTX["t2"]])
finally:
    db.close()

# Fora de banda: outra origem (outro operador / n8n / Conversas) mexe nas tags
# ENQUANTO o editor do primeiro cliente continua aberto com o snapshot antigo
# {t1, t2}.
r_fora_banda = client.put(f"/api/tags/lead/{lead3.id}", json={"tag_ids": [CTX["t1"], CTX["t3"]]})
check(r_fora_banda.status_code == 200, "mudanca fora de banda aplicada (t2 sai, t3 entra)")

# O cliente com o snapshot antigo so ADICIONOU t4 na tela dele — nao mexeu em
# t1/t2, que continuam "marcados" ali. O front-end computa o delta CONTRA O
# SNAPSHOT (ver _lead_edit_modal.html), entao manda adicionar=[t4], remover=[].
r_cliente = client.put(f"/api/tags/lead/{lead3.id}", json={"adicionar": [CTX["t4"]], "remover": []})
check(r_cliente.status_code == 200, f"200 ao aplicar o delta do cliente (veio {r_cliente.status_code})")

final = _tags_do_lead(lead3.id)
check(final == {CTX["t1"], CTX["t3"], CTX["t4"]},
      f"t3 (fora de banda) sobrevive E t4 (do cliente) foi aplicada — nada se perde (tem {final})")
check(CTX["t2"] not in final,
      "t2, removida fora de banda, NAO ressuscita (delta nunca manda tag_ids=[t1,t2,t4])")


# ─── 7. append_anotacao: duas notas sequenciais sobrevivem e ficam ordenadas
print()
print("7) append_anotacao: duas notas sequenciais sobrevivem, mais nova primeiro")

db = SessionLocal()
try:
    lead4 = _lead(db, "Lead Anotacoes")
finally:
    db.close()

r1 = client.put(f"/api/leads/{lead4.id}/anotacoes", params={"texto": "primeira nota"})
check(r1.status_code == 200, f"200 na primeira nota (veio {r1.status_code}: {r1.text[:200]})")
r2 = client.put(f"/api/leads/{lead4.id}/anotacoes", params={"texto": "segunda nota"})
check(r2.status_code == 200, f"200 na segunda nota (veio {r2.status_code}: {r2.text[:200]})")

texto_final = r2.json().get("anotacoes", "") if r2.status_code == 200 else ""
check("primeira nota" in texto_final and "segunda nota" in texto_final,
      f"as DUAS notas sobrevivem no texto final (veio {texto_final!r})")
check(texto_final.find("segunda nota") < texto_final.find("primeira nota"),
      "a nota mais nova aparece PRIMEIRO (mesma ordem de antes do fix)")


# ─── 8. timestamp da anotacao passou a ser UTC-aware (checagem estatica) ──
print()
print("8) [ESTATICO] append_anotacao usa datetime.now(timezone.utc), nao mais naive/local")

leads_py = (ROOT / "app" / "routers" / "leads.py").read_text(encoding="utf-8")
check("datetime.now(timezone.utc)" in leads_py,
      "o timestamp da anotacao usa datetime.now(timezone.utc) em vez de datetime.now() naive")
check("from datetime import datetime, date, timezone" in leads_py,
      "timezone foi importado no topo do arquivo (mesmo padrao UTC-aware do resto do sistema)")


print()
main.app.dependency_overrides.clear()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: contrato de tags (full-replace + delta, C1) e anotacoes concorrentes/UTC (C3)")
