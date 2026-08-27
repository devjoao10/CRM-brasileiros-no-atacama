# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WC — regressao de C6 (W2-12).
AUDIT-2026-08-WF2 — regressao do 404 em lead gravado com formatacao.

GET /api/leads/by-whatsapp/{whatsapp} tinha um terceiro passo de casamento
por sufixo (ilike) resolvido com `.first()` SEM order_by: quando mais de um
lead terminava nos mesmos 11 digitos, quem vencia era indefinido pelo banco e
podia mudar entre execucoes ("localizar lead esta intermitente"). Esta rota e
o primeiro no do fluxo do Formulario e a Tool Buscar Lead WhatsApp do
Gerenciador — um casamento errado atualiza o LEAD DO OUTRO CLIENTE.

WF2: os TRES passos consultavam a coluna CRUA, entao um lead gravado como
`+55 11 98765-4322` (que e o formato que o proprio formulario do site grava)
nao casava em nenhum deles — nem pela string identica. O 404 fazia o
formulario criar lead NOVO em vez de atualizar o existente.

Este arquivo prova que:
- o casamento "stored com +, buscado sem" e "stored sem +, buscado com +"
  continua funcionando (passos 1/2, inalterados);
- buscar pelo numero COMPLETO de um dos dois leads que compartilham os
  mesmos 11 digitos finais devolve o lead CERTO (casamento exato, sem
  ambiguidade);
- buscar so pelos 11 digitos compartilhados (sem DDI, casando os dois so por
  sufixo) devolve 409 — nunca escolhe um dos dois arbitrariamente;
- o corpo do 409 nomeia os ids candidatos, para o operador desambiguar;
- numero inexistente continua 404;
- [WF2] um lead gravado COM FORMATACAO e encontrado por qualquer formato do
  corpus (com/sem `+`, com/sem DDI, com espaco, parenteses e hifen);
- [WF2] o par duplicado que o 404 vinha fabricando (mesmo numero gravado
  formatado e so-digitos em dois leads) agora e visivel e vira 409, nao um
  casamento arbitrario.

Rodar:  python tests/test_leads_lookup_whatsapp.py
        (SQLite por padrao — e o que o CI roda)

Para rodar contra PostgreSQL, exporte DATABASE_URL apontando para um banco
DESCARTAVEL antes de chamar:
        DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/banco \
          python tests/test_leads_lookup_whatsapp.py
Nesse modo o teste NAO limpa a tabela: remove so os numeros que ele mesmo
semeia, para nunca destruir dados de quem apontou o DATABASE_URL errado.
"""
import pathlib
import sys
from urllib.parse import quote

ROOT = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))
import os  # noqa: E402

# AUDIT-2026-08-WF2 — SQLite e o default (o CI roda assim), mas um
# DATABASE_URL ja definido pelo operador tem precedencia: a correcao deste
# arquivo depende de dialeto (`regexp_replace` no PostgreSQL, cadeia de
# `replace()` no SQLite) e os DOIS precisam passar.
_URL_EXTERNA = os.environ.get("DATABASE_URL", "")
USANDO_SQLITE = not _URL_EXTERNA or _URL_EXTERNA.startswith("sqlite")

if USANDO_SQLITE:
    SCRATCH = ROOT / "scratch"
    SCRATCH.mkdir(exist_ok=True)
    DB_FILE = SCRATCH / "leads_lookup_whatsapp_test.db"
    if DB_FILE.exists():
        DB_FILE.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["SEED_INITIAL_ADMIN"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base, IS_SQLITE  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.user import UserRole  # noqa: E402

Base.metadata.create_all(bind=engine)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

print(f"dialeto: {'SQLite' if IS_SQLITE else 'PostgreSQL'}")
print()

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


# Todos os numeros que este arquivo semeia. Usado para limpar restos de uma
# execucao anterior sem tocar em nenhuma outra linha da tabela.
WPP_A = "5511987654321"
WPP_B = "5411987654321"
WPP_COM_MAIS = "+5548999998888"
WPP_SEM_MAIS = "5548999997777"
WPP_FORMATADO = "+55 11 98765-4322"      # WF2: o que o formulario do site grava
WPP_DUP_FORMATADO = "+55 11 91111-2233"  # WF2: par duplicado formatado...
WPP_DUP_DIGITOS = "5511911112233"        # ...e o mesmo numero so em digitos
SEMEADOS = [WPP_A, WPP_B, WPP_COM_MAIS, WPP_SEM_MAIS, WPP_FORMATADO,
            WPP_DUP_FORMATADO, WPP_DUP_DIGITOS]

db = SessionLocal()
try:
    db.query(Lead).filter(Lead.whatsapp.in_(SEMEADOS)).delete(synchronize_session=False)
    db.commit()

    # Dois leads REAIS e distintos cujos ultimos 11 digitos coincidem, com
    # "DDI" diferente nos dois primeiros digitos (55 vs 54) — exatamente o
    # tipo de colisao que o .first() antigo resolvia arbitrariamente.
    ID_A = _lead_id(db, "Cliente Brasil", WPP_A)
    ID_B = _lead_id(db, "Cliente Outro Pais", WPP_B)
    # Um lead guardado COM "+" (import/webhook às vezes grava assim).
    ID_COM_MAIS = _lead_id(db, "Lead Com Mais", WPP_COM_MAIS)
    # Um lead guardado SEM "+".
    ID_SEM_MAIS = _lead_id(db, "Lead Sem Mais", WPP_SEM_MAIS)
    # AUDIT-2026-08-WF2 — lead gravado com a formatacao do formulario do site.
    ID_FORMATADO = _lead_id(db, "Cliente do Formulario", WPP_FORMATADO)
    # AUDIT-2026-08-WF2 — a duplicata que o 404 vinha fabricando.
    ID_DUP_FORMATADO = _lead_id(db, "Duplicata Formatada", WPP_DUP_FORMATADO)
    ID_DUP_DIGITOS = _lead_id(db, "Duplicata So Digitos", WPP_DUP_DIGITOS)
finally:
    db.close()


def get_por_whatsapp(numero):
    """GET na rota com o numero escapado — o corpus tem espaco, `+` e `()`."""
    return client.get(f"/api/leads/by-whatsapp/{quote(numero, safe='')}")


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


# ─── 5. [REGRESSAO WF2] lead gravado FORMATADO e achado em qualquer formato
print()
print("5) [REGRESSAO WF2] lead gravado como '+55 11 98765-4322' e encontrado")
print("   por todo o corpus de formatos — antes eram 404 os seis, inclusive a")
print("   busca pela string identica a que estava gravada")

CORPUS = [
    "+55 11 98765-4322",   # identica a gravada
    "(11) 98765-4322",     # so DDD, com parenteses (11 digitos)
    "11987654322",         # so DDD, so digitos (11 digitos)
    "5511987654322",       # com DDI, so digitos (13 digitos)
    "+5511987654322",      # com DDI e '+'
    "55 11 9 8765 4322",   # com DDI e espacos
]
for numero in CORPUS:
    r = get_por_whatsapp(numero)
    detalhe = "" if r.status_code == 200 else f": {r.text[:160]}"
    check(r.status_code == 200 and r.json().get("id") == ID_FORMATADO,
          f"{numero!r} devolve o lead formatado (id={ID_FORMATADO}) "
          f"(veio {r.status_code}{detalhe})")


# ─── 6. [WF2] a duplicata que o 404 fabricava vira 409, nao casamento torto
print()
print("6) [WF2] mesmo numero em DOIS leads (um formatado, um so digitos) — a")
print("   duplicata que o proprio 404 vinha fabricando — e 409, nao um")
print("   casamento arbitrario entre os dois")

r = client.get(f"/api/leads/by-whatsapp/{WPP_DUP_DIGITOS}")
check(r.status_code == 409,
      f"409 para o numero que dois leads compartilham em formatos diferentes (veio {r.status_code})")
detalhe = r.json().get("detail", "") if r.status_code == 409 else ""
check(str(ID_DUP_FORMATADO) in detalhe and str(ID_DUP_DIGITOS) in detalhe,
      f"o 409 nomeia os dois ids duplicados (detail={detalhe!r})")


print()
main.app.dependency_overrides.clear()

if not USANDO_SQLITE:
    # Banco externo: devolve a tabela ao estado anterior.
    db = SessionLocal()
    try:
        db.query(Lead).filter(Lead.whatsapp.in_(SEMEADOS)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: GET /api/leads/by-whatsapp acha lead formatado (WF2) e recusa ambiguidade (C6)")
