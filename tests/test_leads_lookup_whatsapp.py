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
- buscar por um valor que casa DOIS leads so pelo sufixo de 11 digitos (numeros
  que nenhum lado le como brasileiro) devolve 409 — nunca escolhe um dos dois
  arbitrariamente; quando so um dos dois e a mesma linha (um le como
  brasileiro, o outro so coincide nos digitos), devolve esse;
- o corpo do 409 nomeia os ids candidatos, para o operador desambiguar;
- numero inexistente continua 404;
- [WF2] um lead gravado COM FORMATACAO e encontrado por qualquer formato do
  corpus (com/sem `+`, com/sem DDI, com espaco, parenteses e hifen);
- [WF2] o par duplicado que o 404 vinha fabricando (mesmo numero gravado
  formatado e so-digitos em dois leads) agora e visivel e vira 409, nao um
  casamento arbitrario;
- [CONV-LEAD-FONE-01] o MESMO celular em outra grafia e a mesma linha: o modal
  do CRM grava 5531987654321 e a Meta/BIA entregam 553187654321 (DDD fora de
  11-19, 21, 22, 24, 27 e 28 chega sem o nono digito). A busca acha o lead nos
  dois sentidos e em qualquer formatacao guardada;
- [CONV-LEAD-FONE-01] a regra nao reabre o F10: DDD diferente NUNCA casa (o
  sufixo antigo cruzava 554987654321 com 54987654321), fixo nao casa com
  celular de mesmo final, numero sem DDD e os 10 ultimos digitos nao
  identificam ninguem;
- [CONV-LEAD-FONE-01] a igualdade exata continua com precedencia (200 com o
  lead exato, nao 409) e dois leads compativeis SEM exato continuam 409;
- [CONV-LEAD-FONE-01] o sufixo de 11 digitos so vale quando NENHUM dos dois
  numeros le como brasileiro, e o LIKE incide sobre os digitos NORMALIZADOS da
  coluna: `+56 9 8765 4321` e achado por `5556987654321`. Com UM lado de leitura
  brasileira so a leitura canonica decide: estrangeiro ou registro corrompido
  nao casa com brasileiro pelo sufixo (Chile x DDD 69 sem o 9: 404);
- [CONV-LEAD-FONE-01] pre-filtro do passo 2 que enche o limite de candidatos e
  ambiguo: 409 pedindo desambiguacao manual, nunca um lead eleito numa amostra.

Rodar:  python tests/test_leads_lookup_whatsapp.py
        (SQLite por padrao — e o que o CI roda)

Para rodar contra PostgreSQL, exporte DATABASE_URL apontando para um banco
DESCARTAVEL antes de chamar:
        DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/banco \
          python tests/test_leads_lookup_whatsapp.py
Nesse modo o teste NAO limpa a tabela: remove so o que ele mesmo semeia — os
numeros de SEMEADOS e os leads temporarios dos cenarios 7 em diante (por id; os de
uma execucao interrompida, pelo nome) —, para nunca destruir dados de quem apontou
o DATABASE_URL errado.
"""
import pathlib
import sys
from contextlib import contextmanager
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
import app.routers.leads as rota_leads  # noqa: E402
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

# CONV-LEAD-FONE-01 — a MESMA linha em outra grafia. DDD 31 esta fora dos DDDs em
# que a Meta entrega o nono digito (11-19, 21, 22, 24, 27, 28): o wa_id chega como
# SEM_NONO e o modal do CRM grava COM_NONO. Os cenarios 7 a 16 semeiam os leads na
# hora, um grupo por cenario (`leads_temporarios`): o par "exato x variante" de um
# nao pode virar o 409 do outro. Esses leads se identificam por este nome, que e
# tambem o que a limpeza de uma execucao interrompida procura - nunca um numero,
# nunca a tabela inteira.
SEM_NONO = "553187654321"       # como a Meta/BIA entregam (12 digitos)
COM_NONO = "5531987654321"      # como o modal do CRM grava (13 digitos)
NOME_TEMPORARIO = "CONV-LEAD-FONE-01 lead de teste"

db = SessionLocal()
try:
    db.query(Lead).filter(Lead.whatsapp.in_(SEMEADOS)).delete(synchronize_session=False)
    db.query(Lead).filter(Lead.nome == NOME_TEMPORARIO).delete(synchronize_session=False)
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


@contextmanager
def leads_temporarios(*numeros):
    """
    CONV-LEAD-FONE-01 — semeia um lead por numero e os REMOVE ao sair, mesmo se
    o bloco levantar. Devolve os ids na ordem dos numeros. Apaga so o que criou
    (por id), entao nunca toca em lead alheio num banco externo.
    """
    ids = []
    try:
        with SessionLocal() as s:
            for numero in numeros:
                ids.append(_lead_id(s, NOME_TEMPORARIO, numero))
        yield ids
    finally:
        if ids:
            with SessionLocal() as s:
                s.query(Lead).filter(Lead.id.in_(ids)).delete(synchronize_session=False)
                s.commit()


def check_acha(buscado, lead_id, rotulo):
    """200 com o lead esperado; quando falha, a mensagem traz o status e o corpo."""
    r = get_por_whatsapp(buscado)
    detalhe = "" if r.status_code == 200 else f": {r.text[:160]}"
    check(r.status_code == 200 and r.json().get("id") == lead_id,
          f"{rotulo}: {buscado!r} devolve o lead (id={lead_id}) "
          f"(veio {r.status_code}{detalhe})")


def check_nao_acha(buscado, rotulo):
    """404: nenhum lead e a mesma linha - quem chama cria um lead novo, nunca atualiza o alheio."""
    r = get_por_whatsapp(buscado)
    check(r.status_code == 404,
          f"{rotulo}: {buscado!r} e 404 (veio {r.status_code}: {r.text[:120]})")


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


# ─── 3. [REGRESSAO] colisao de sufixo: 409, nunca um lead escolhido ───────
print()
print("3) [REGRESSAO C6] dois leads que so o sufixo de 11 digitos casa com a busca e 409, nunca 200")

# CONV-LEAD-FONE-01: o sufixo so junta numeros que NENHUM dos dois le como brasileiro.
# A busca `56987654321` (DDD 56 nao existe) e os leads `5556987654321` e `0056987654321`
# (13 digitos, sem leitura) terminam nos mesmos 11 digitos: dois candidatos legitimos do
# sufixo. Este e o caso do .first() sem order_by que o C6 corrigiu.
with leads_temporarios("5556987654321", "0056987654321") as (id_um, id_dois):
    r = get_por_whatsapp("56987654321")
    check(r.status_code == 409,
          f"409 para um valor que so casa os dois por sufixo — NUNCA 200 escolhendo um "
          f"arbitrariamente (veio {r.status_code})")
    detalhe = r.json().get("detail", "") if r.status_code == 409 else ""
    check(str(id_um) in detalhe and str(id_dois) in detalhe,
          f"o corpo do 409 nomeia os DOIS ids candidatos, para o operador desambiguar "
          f"(detail={detalhe!r})")

# O fixture historico: A (`5511987654321`) e B (`5411987654321`) tambem terminam nos mesmos
# 11 digitos, mas A le como brasileiro (DDD 11) e B nao. Antes, o sufixo juntava os dois e a
# busca por `11987654321` era 409; agora so A e a MESMA linha da busca e B e so uma
# coincidencia de digitos (o sufixo alinharia um brasileiro com um numero de outro pais).
suffix_comum = WPP_A[-11:]
check(suffix_comum == WPP_B[-11:], "sanidade do seed: A e B compartilham os ultimos 11 digitos")
check_acha(suffix_comum, ID_A, "A (le como brasileiro) e a unica mesma linha; B so coincide nos digitos")


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


# ─── 7. [CONV-LEAD-FONE-01] busca SEM o nono digito acha o lead gravado COM ele
print()
print("7) [CONV-LEAD-FONE-01] a Meta/BIA entregam 553187654321 (sem o nono digito);")
print("   o lead que o modal do CRM gravou com ele e achado em qualquer formatacao")

for gravado in (COM_NONO, "(31) 98765-4321", "+55 31 98765-4321"):
    with leads_temporarios(gravado) as (lead_id,):
        check_acha(SEM_NONO, lead_id, f"lead gravado como {gravado!r}")


# ─── 8. [CONV-LEAD-FONE-01] o inverso: lead legado SEM o nono digito ──────
print()
print("8) [CONV-LEAD-FONE-01] o inverso: o lead legado 553187654321 (a BIA e o")
print("   auto-create gravam o wa_id cru) e achado pela busca COM o nono digito")

with leads_temporarios(SEM_NONO) as (lead_id,):
    for buscado in (COM_NONO, "+55 31 98765-4321", "(31) 98765-4321"):
        check_acha(buscado, lead_id, "lead legado sem o nono digito")


# ─── 9. [CONV-LEAD-FONE-01/F10] DDD diferente NUNCA casa ──────────────────
print()
print("9) [CONV-LEAD-FONE-01/F10] DDD diferente NUNCA casa - nem onde o sufixo de 11")
print("   digitos alinhava errado (554987654321 termina em 54987654321: DDD 49 x 54)")

with leads_temporarios(COM_NONO):
    check_nao_acha("5547987654321", "mesmo assinante, outro DDD (47 buscado x 31 gravado)")
    check_nao_acha("554787654321", "idem, sem o nono digito")
for gravado, buscado, rotulo in (
    ("54987654321", "554987654321", "DDD 54 gravado x 49 buscado"),
    ("554987654321", "54987654321", "DDD 49 gravado x 54 buscado"),
    ("51987654321", "551987654321", "DDD 51 gravado x 19 buscado"),
):
    with leads_temporarios(gravado):
        check_nao_acha(buscado, rotulo)


# ─── 10. [CONV-LEAD-FONE-01/F10] fixo x celular ───────────────────────────
print()
print("10) [CONV-LEAD-FONE-01/F10] fixo nunca casa com celular de mesmo final (o fixo")
print("    nunca ganha o nono digito) - mas casa consigo mesmo em outra grafia")

FIXO_31 = "553133334444"        # (31) 3333-4444
CELULAR_31 = "5531933334444"    # (31) 93333-4444: mesmos 8 digitos finais do fixo
with leads_temporarios(FIXO_31) as (id_fixo,):
    check_nao_acha(CELULAR_31, "fixo gravado x celular buscado")
    check_acha("(31) 3333-4444", id_fixo, "controle positivo: o fixo buscado sem DDI")
with leads_temporarios(CELULAR_31):
    check_nao_acha(FIXO_31, "celular gravado x fixo buscado")


# ─── 11. [CONV-LEAD-FONE-01] igualdade exata tem precedencia ──────────────
print()
print("11) [CONV-LEAD-FONE-01] igualdade EXATA tem precedencia sobre a outra grafia:")
print("    com os dois leads no banco a busca devolve o exato (200), nao 409")

with leads_temporarios(SEM_NONO, COM_NONO) as (id_sem_nono, id_com_nono):
    check_acha(SEM_NONO, id_sem_nono, "busca sem o nono digito -> o lead gravado igual")
    check_acha(COM_NONO, id_com_nono, "busca com o nono digito -> o lead gravado igual")


# ─── 12. [CONV-LEAD-FONE-01] dois compativeis sem exato continuam 409 ─────
print()
print("12) [CONV-LEAD-FONE-01] dois leads da mesma linha e NENHUM exato: 409 nomeando")
print("    os dois - nunca escolher um arbitrariamente")

with leads_temporarios(COM_NONO, "31987654321") as (id_com_ddi, id_sem_ddi):
    r = get_por_whatsapp(SEM_NONO)
    check(r.status_code == 409,
          f"409 para a linha que dois leads guardam em grafias diferentes (veio {r.status_code})")
    detalhe = r.json().get("detail", "") if r.status_code == 409 else ""
    check(f"{sorted([id_com_ddi, id_sem_ddi])}" in detalhe,
          f"o 409 nomeia os dois ids (detail={detalhe!r})")


# ─── 13. [CONV-LEAD-FONE-01/F10] sem o piso de 11 digitos, sem DDD nao casa
print()
print("13) [CONV-LEAD-FONE-01/F10] o passo 2 deixou de exigir 11 digitos: DDD + 8")
print("    digitos acha o lead, mas numero sem DDD continua sem casar com ninguem")

with leads_temporarios(COM_NONO, "5521987654321") as (id_31, _id_21):
    check_acha("3187654321", id_31, "DDD 31 + 8 digitos (celular antigo, sem DDI)")
    check_nao_acha("987654321", "9 digitos, sem DDD")
    check_nao_acha("87654321", "8 digitos, sem DDD")
    check_nao_acha("1987654321", "os 10 ultimos digitos dos DOIS leads (DDD 31 e 21), lidos como DDD 19")


# ─── 14. [CONV-LEAD-FONE-01] sufixo (b) entre numeros sem leitura brasileira ─
print()
print("14) [CONV-LEAD-FONE-01] regra (b): o sufixo de 11 digitos junta numeros que NENHUM dos")
print("    dois le como brasileiro - e o LIKE incide sobre os digitos NORMALIZADOS da coluna")

# `5556987654321` nao le (DDD 56 nao existe) e o lead `+56 9 8765 4321` tambem nao
# (`56987654321`): (b) os junta. O pre-filtro so traz o lead pelo LIKE sobre a expressao
# normalizada - o texto cru (`+`, espacos) jamais terminaria nesses 11 digitos -, entao
# este cenario reprova qualquer LIKE aplicado a coluna crua.
with leads_temporarios("+56 9 8765 4321") as (id_formatado,):
    check_acha("5556987654321", id_formatado, "lead gravado formatado, sem leitura dos dois lados")


# ─── 15. [CONV-LEAD-FONE-01] com um lado brasileiro so a leitura decide ───
print()
print("15) [CONV-LEAD-FONE-01] com UM lado de leitura brasileira so a leitura canonica decide:")
print("    o sufixo coincidente de estrangeiro/registro corrompido nao junta pessoas diferentes")

# Em todos os pares os 11 ultimos digitos coincidem (o pre-filtro devolve o lead), mas
# so um lado le como brasileiro. Antes, o sufixo devolvia 200 com o lead errado.
for gravado, buscado, rotulo in (
    ("56987654321", "556987654321", "Chile gravado x DDD 69 sem o 9 buscado"),
    ("556987654321", "56987654321", "DDD 69 sem o 9 gravado x Chile buscado"),
    ("5555489887117", "555489887117",
     "registro corrompido pelo modal antigo x numero real do DDD 54"),
    ("555489887117", "5555489887117", "idem, sentido inverso"),
):
    with leads_temporarios(gravado):
        check_nao_acha(buscado, rotulo)


# ─── 16. [CONV-LEAD-FONE-01] pre-filtro cheio e AMBIGUO (fail-closed no LIMIT)
print()
print("16) [CONV-LEAD-FONE-01] se o pre-filtro do passo 2 enche o limite de candidatos o Python")
print("    decidiria sobre uma AMOSTRA: 409, nunca um lead eleito nela")

LIMITE = rota_leads._LIMITE_CANDIDATOS_WHATSAPP
BUSCADO_LIMITE = "5541987654321"
LEGITIMO_LIMITE = "(41) 98765-4321"   # a mesma linha, gravada sem DDI


def ruido_limite(quantos):
    """Leads que o pre-filtro traz (terminam nos 11 ultimos digitos de BUSCADO_LIMITE) e
    que o Python recusa (14 digitos, nenhum le como brasileiro). Entram ANTES do
    legitimo: e o que o LIMIT corta quando o pre-filtro enche."""
    return [f"{900 + i}41987654321" for i in range(quantos)]


with leads_temporarios(*ruido_limite(LIMITE - 2), LEGITIMO_LIMITE) as ids:
    check_acha(BUSCADO_LIMITE, ids[-1],
               f"{LIMITE - 1} candidatos (o pre-filtro nao enche o limite de {LIMITE}): decide")
for quantos, rotulo in (
    (LIMITE - 1, f"exatamente {LIMITE} candidatos (nao da para saber se truncou)"),
    (LIMITE, f"{LIMITE + 1} candidatos (truncado: o legitimo ficou de fora)"),
):
    with leads_temporarios(*ruido_limite(quantos), LEGITIMO_LIMITE):
        r = get_por_whatsapp(BUSCADO_LIMITE)
        detalhe = r.json().get("detail", "") if r.status_code == 409 else ""
        check(r.status_code == 409,
              f"{rotulo}: 409, nunca 200/404 sobre uma amostra (veio {r.status_code}: {r.text[:120]})")
        check("Candidatos demais" in detalhe and "Desambigue manualmente" in detalhe,
              f"{rotulo}: o corpo do 409 diz que ha candidatos demais e pede desambiguacao "
              f"manual (detail={detalhe!r})")


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
print("OK: GET /api/leads/by-whatsapp acha lead formatado (WF2), recusa ambiguidade (C6) "
      "e reconhece a mesma linha em outra grafia sem cruzar DDD nem decidir sobre "
      "amostra truncada (CONV-LEAD-FONE-01)")
