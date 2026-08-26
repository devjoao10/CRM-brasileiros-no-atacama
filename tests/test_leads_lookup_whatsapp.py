# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WC — regressao de C6 (W2-12).

GET /api/leads/by-whatsapp/{whatsapp} tinha um terceiro passo de casamento
por sufixo (ilike) resolvido com `.first()` SEM order_by: quando mais de um
lead terminava nos mesmos 11 digitos, quem vencia era indefinido pelo banco e
podia mudar entre execucoes ("localizar lead esta intermitente"). Esta rota e
o primeiro no do fluxo do Formulario e a Tool Buscar Lead WhatsApp do
Gerenciador — um casamento errado atualiza o LEAD DO OUTRO CLIENTE.

Este arquivo prova que:
- o casamento "stored com +, buscado sem" e "stored sem +, buscado com +"
  continua funcionando (passos 1/2, inalterados);
- buscar pelo numero COMPLETO de um dos dois leads que compartilham os
  mesmos 11 digitos finais devolve o lead CERTO (casamento exato, sem
  ambiguidade);
- buscar so pelos 11 digitos compartilhados (sem DDI, casando os dois so por
  sufixo) devolve 409 — nunca escolhe um dos dois arbitrariamente;
- o corpo do 409 nomeia os ids candidatos, para o operador desambiguar;
- numero inexistente continua 404.

Rodar:  python tests/test_leads_lookup_whatsapp.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "leads_lookup_whatsapp_test.db"
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


def _lead_id(db, nome, whatsapp):
    """Cria o lead e devolve so o id (int) — evitar guardar o objeto ORM: o
    proximo _lead_id() na MESMA sessao comita de novo e EXPIRA os atributos
    dos leads anteriores, que viram DetachedInstanceError quando lidos depois
    que a sessao fecha."""
    lead = Lead(nome=nome, whatsapp=whatsapp)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead.id


db = SessionLocal()
try:
    # Dois leads REAIS e distintos cujos ultimos 11 digitos coincidem, com
    # "DDI" diferente nos dois primeiros digitos (55 vs 54) — exatamente o
    # tipo de colisao que o .first() antigo resolvia arbitrariamente.
    ID_A = _lead_id(db, "Cliente Brasil", "5511987654321")
    ID_B = _lead_id(db, "Cliente Outro Pais", "5411987654321")
    # Um lead guardado COM "+" (import/webhook às vezes grava assim).
    ID_COM_MAIS = _lead_id(db, "Lead Com Mais", "+5548999998888")
    # Um lead guardado SEM "+".
    ID_SEM_MAIS = _lead_id(db, "Lead Sem Mais", "5548999997777")
finally:
    db.close()

WPP_A = "5511987654321"
WPP_B = "5411987654321"


# ─── 1. stored com "+", buscado sem — e vice-versa (passos 1/2, inalterados)
print("1) stored com '+' buscado sem, e stored sem '+' buscado com '+', ambos 200")

r = client.get("/api/leads/by-whatsapp/5548999998888")
check(r.status_code == 200, f"200 buscando sem '+' um numero guardado COM '+' (veio {r.status_code})")
check(r.status_code == 200 and r.json()["id"] == ID_COM_MAIS,
      "devolveu o lead certo (guardado com '+')")

r = client.get("/api/leads/by-whatsapp/+5548999997777")
check(r.status_code == 200, f"200 buscando com '+' um numero guardado SEM '+' (veio {r.status_code})")
check(r.status_code == 200 and r.json()["id"] == ID_SEM_MAIS,
      "devolveu o lead certo (guardado sem '+')")


# ─── 2. buscar pelo numero COMPLETO de um dos dois desambigua sozinho ────
print()
print("2) buscar pelo numero completo de A ou de B devolve o lead CERTO (casamento exato)")

r = client.get(f"/api/leads/by-whatsapp/{WPP_A}")
check(r.status_code == 200, f"200 buscando o numero completo de A (veio {r.status_code}: {r.text[:200]})")
check(r.status_code == 200 and r.json()["id"] == ID_A,
      f"devolveu A (id={ID_A}), nao B, mesmo os dois compartilhando os ultimos 11 digitos")

r = client.get(f"/api/leads/by-whatsapp/{WPP_B}")
check(r.status_code == 200, f"200 buscando o numero completo de B (veio {r.status_code}: {r.text[:200]})")
check(r.status_code == 200 and r.json()["id"] == ID_B,
      f"devolveu B (id={ID_B}), nao A")


# ─── 3. [REGRESSAO] buscar so pelo sufixo compartilhado (sem DDI) e 409 ──
print()
print("3) [REGRESSAO] buscar so pelos 11 digitos compartilhados por A e B e 409, nunca 200")

suffix_comum = WPP_A[-11:]
check(suffix_comum == WPP_B[-11:], "sanidade do seed: A e B compartilham os ultimos 11 digitos")

r = client.get(f"/api/leads/by-whatsapp/{suffix_comum}")
check(r.status_code == 409,
      f"409 para um valor que so caso os dois por sufixo — NUNCA 200 escolhendo um arbitrariamente (veio {r.status_code})")
detalhe = r.json().get("detail", "") if r.status_code == 409 else ""
check(str(ID_A) in detalhe and str(ID_B) in detalhe,
      f"o corpo do 409 nomeia os DOIS ids candidatos, para o operador desambiguar (detail={detalhe!r})")


# ─── 4. numero inexistente continua 404 ──────────────────────────────────
print()
print("4) numero que nenhum lead tem continua 404")

r = client.get("/api/leads/by-whatsapp/5599999999999")
check(r.status_code == 404, f"404 para numero inexistente (veio {r.status_code})")


print()
main.app.dependency_overrides.clear()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: GET /api/leads/by-whatsapp recusa ambiguidade em vez de escolher (C6)")
