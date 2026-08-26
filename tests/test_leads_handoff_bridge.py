"""
AUDIT-2026-08-WA — a ponte CRM -> Conversas no handoff.

CONTEXTO DO DEFEITO
-------------------
`POST /api/conversations/{id}/handoff` existe no Conversas, esta correto, e nao
tinha chamador: nenhum dos 18 nos do workflow "Agente Gerenciador de Leads"
alcanca a porta 8001 (confirmado nos exports de 2026-08-26). A conversa ficava
em ATENDIMENTOS BIA com o bot ligado enquanto a Bia dizia ao cliente que ele
tinha entrado na fila.

O unico sinal deterministico que o repositorio recebe naquele instante e
`PUT /api/leads/{lead_id}/responsavel` (`Tool Alterar Responsavel`, com
`responsavel_id=5` fixo na URL do n8n). Esta suite trava o comportamento da
ponte que parte dali.

O QUE ESTE ARQUIVO PROVA
------------------------
  1. Trocar o responsavel para uma PESSOA dispara a ponte, na URL certa, com a
     credencial certa.
  2. Devolver o lead ao Agente IA (`responsavel_id` ausente/null) NAO dispara —
     mover a conversa para a fila humana ali seria o oposto da intencao.
  3. Reatribuir para o MESMO responsavel AINDA dispara. O n8n manda o id FIXO e
     nada devolve `lead.responsavel_id` a NULL quando a conversa encerra, mas o
     Conversas reseta a conversa para a Bia quando o cliente volta a escrever —
     entao pular por "nada mudou" prendia o cliente que retorna na Bia.
  4. Sem `CONVERSAS_API_KEY` a ponte e no-op e nao faz requisicao nenhuma — o
     comportamento de hoje, para que nenhum ambiente regrida por nao configurar.
  5. Conversas fora do ar / timeout / 500: o `PUT` continua 200 e o responsavel
     FICA salvo. Uma degradacao do inbox nao pode virar perda de dado do lead.
  6. 404 do Conversas (lead sem conversa aberta, ex.: veio de formulario) e
     resultado normal, nao falha.

O cliente HTTP e substituido; nenhuma rede e tocada.

Roda standalone:  python tests/test_leads_handoff_bridge.py
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "leads_handoff_bridge_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["CONVERSAS_BASE_URL"] = "http://conversas-de-teste:8001"
os.environ["CONVERSAS_API_KEY"] = "chave-de-teste"

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import get_current_user  # noqa: E402
from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.services import conversas_bridge  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

db = SessionLocal()
julia = User(nome="Julia", email="julia@teste.local", hashed_password="x",
             role=UserRole.USER, is_active=True, email_verified=True)
beto = User(nome="Beto", email="beto@teste.local", hashed_password="x",
            role=UserRole.USER, is_active=True, email_verified=True)
db.add_all([julia, beto])
db.commit()
JULIA_ID, BETO_ID = julia.id, beto.id

lead = Lead(nome="Cliente Teste", whatsapp="5511900000001")
db.add(lead)
db.commit()
LEAD_ID = lead.id
db.close()


def _override_user():
    d = SessionLocal()
    try:
        yield d
    finally:
        d.close()


app.dependency_overrides[get_db] = _override_user
app.dependency_overrides[get_current_user] = lambda: julia
client = TestClient(app)


# ─── cliente HTTP falso: registra as chamadas, nunca toca a rede ──────
chamadas = []
_resposta_programada = {"status": 200, "excecao": None}


class _RespostaFalsa:
    def __init__(self, status_code):
        self.status_code = status_code


class _ClienteFalso:
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, **kwargs):
        chamadas.append({"url": url, "headers": headers or {}})
        if _resposta_programada["excecao"] is not None:
            raise _resposta_programada["excecao"]
        return _RespostaFalsa(_resposta_programada["status"])


conversas_bridge.httpx.AsyncClient = _ClienteFalso


def trocar_responsavel(novo_id):
    chamadas.clear()
    url = f"/api/leads/{LEAD_ID}/responsavel"
    if novo_id is not None:
        url += f"?responsavel_id={novo_id}"
    return client.put(url)


def responsavel_atual():
    d = SessionLocal()
    try:
        return d.query(Lead).filter(Lead.id == LEAD_ID).first().responsavel_id
    finally:
        d.close()


# ============ 1. troca para pessoa dispara a ponte ============
print("1 — responsavel vira uma PESSOA")

_resposta_programada.update(status=200, excecao=None)
r = trocar_responsavel(JULIA_ID)
check(r.status_code == 200, f"PUT devolve 200 (got {r.status_code})")
check(responsavel_atual() == JULIA_ID, "responsavel salvo no lead")
check(len(chamadas) == 1, f"a ponte foi chamada exatamente uma vez (got {len(chamadas)})")
if chamadas:
    check(chamadas[0]["url"] ==
          f"http://conversas-de-teste:8001/api/conversations/by-lead/{LEAD_ID}/handoff",
          f"URL correta (got {chamadas[0]['url']})")
    check(chamadas[0]["headers"].get("X-API-Key") == "chave-de-teste",
          "credencial vai no header X-API-Key")
    check("localhost" not in chamadas[0]["url"],
          "usa CONVERSAS_BASE_URL da config, nao um host escrito a mao")


# ============ 2. voltar para o Agente IA NAO dispara ============
print("\n2 — responsavel volta a ser o Agente IA")

r = trocar_responsavel(None)
check(r.status_code == 200, "PUT devolve 200")
check(responsavel_atual() is None, "responsavel volta a NULL (Agente IA)")
check(len(chamadas) == 0,
      f"a ponte NAO e chamada (got {len(chamadas)}) — devolver a IA nao e entrar na fila humana")


# ======= 3. reatribuir para o MESMO responsavel AINDA dispara =======
# AUDIT-2026-08-WA (revisao) — ESTA ASSERCAO ESTAVA INVERTIDA, e era o defeito.
#
# A versao anterior condicionava a ponte a `old_responsavel != responsavel_id`, e
# este teste afirmava "nada mudou -> ponte nao e chamada". Parecia economia. Na
# pratica desligava a ponte em quase todo handoff real:
#
#   - o n8n manda `?responsavel_id=5` FIXO;
#   - nada no CRM devolve `lead.responsavel_id` para NULL quando a conversa encerra;
#   - mas o Conversas RESETA a conversa para a Bia quando um cliente encerrado
#     volta a escrever.
#
# Logo, no SEGUNDO handoff do mesmo lead o CRM via `5 == 5`, pulava a ponte, e a
# conversa ficava presa em ATENDIMENTOS BIA — o defeito original de volta, e em
# silencio. Cliente que volta a escrever nao e caso raro: e o caso comum.
print()
print("3 — reatribuicao para o mesmo responsavel")

trocar_responsavel(BETO_ID)          # primeira atribuicao
r = trocar_responsavel(BETO_ID)      # MESMO valor: a conversa pode ter voltado para a Bia
check(r.status_code == 200, "PUT devolve 200")
check(len(chamadas) == 1,
      f"mesmo responsavel -> a ponte AINDA e chamada (got {len(chamadas)}); "
      f"o handoff do outro lado e idempotente, e pular aqui prendia o cliente na Bia")


# ============ 4. sem credencial: no-op de verdade ============
print("\n4 — ponte desligada (CONVERSAS_API_KEY vazia)")

original = conversas_bridge.CONVERSAS_API_KEY
conversas_bridge.CONVERSAS_API_KEY = ""
try:
    r = trocar_responsavel(JULIA_ID)
    check(r.status_code == 200, "PUT continua 200")
    check(responsavel_atual() == JULIA_ID, "responsavel salvo mesmo com a ponte desligada")
    check(len(chamadas) == 0,
          f"NENHUMA requisicao e feita (got {len(chamadas)}) — nao adivinha credencial")
finally:
    conversas_bridge.CONVERSAS_API_KEY = original


# ============ 5. Conversas fora do ar nao derruba o lead ============
print("\n5 — Conversas indisponivel")

for nome, prog in [
    ("timeout de rede", {"status": 200, "excecao": TimeoutError("timeout simulado")}),
    ("conexao recusada", {"status": 200, "excecao": ConnectionError("recusada")}),
    ("HTTP 500", {"status": 500, "excecao": None}),
    ("HTTP 401", {"status": 401, "excecao": None}),
]:
    trocar_responsavel(None)                       # zera para forcar mudanca real
    _resposta_programada.update(prog)
    r = trocar_responsavel(BETO_ID)
    check(r.status_code == 200, f"{nome}: PUT continua 200 (got {r.status_code})")
    check(responsavel_atual() == BETO_ID,
          f"{nome}: responsavel FICA salvo — degradacao do inbox nao vira perda de dado")

_resposta_programada.update(status=200, excecao=None)


# ============ 6. 404 e resultado normal ============
print("\n6 — lead sem conversa aberta (404)")

trocar_responsavel(None)
_resposta_programada.update(status=404, excecao=None)
r = trocar_responsavel(JULIA_ID)
check(r.status_code == 200, "PUT devolve 200 — 404 do inbox nao e erro do lead")
check(responsavel_atual() == JULIA_ID, "responsavel salvo")
check(len(chamadas) == 1, "a ponte foi tentada")

resultado = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
    conversas_bridge.notificar_handoff(LEAD_ID)
)
check(resultado is False,
      "404 devolve False (respondeu, nao havia conversa), nunca None nem excecao")

_resposta_programada.update(status=200, excecao=None)
resultado = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
    conversas_bridge.notificar_handoff(LEAD_ID)
)
check(resultado is True, "200 devolve True")

_resposta_programada.update(status=200, excecao=RuntimeError("caiu"))
resultado = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
    conversas_bridge.notificar_handoff(LEAD_ID)
)
check(resultado is None, "falha de rede devolve None, e NAO levanta")
_resposta_programada.update(status=200, excecao=None)


# ============ 7. guards de codigo ============
print("\n7 — a ponte nao escreve na tabela do outro servico")

fonte = (ROOT / "app" / "services" / "conversas_bridge.py").read_text(encoding="utf-8")
# Comentario/docstring podem citar a tabela; o que nao pode existir e ACESSO.
codigo = "\n".join(
    linha for linha in fonte.splitlines()
    if not linha.lstrip().startswith("#")
)
for proibido in ("sqlalchemy", "text(", ".execute(", "Session", "get_db"):
    check(proibido not in codigo,
          f"a ponte nao importa nem usa `{proibido}` — quem escreve em "
          f"`conversations` e o dono da tabela, via a rota dele")
check("raise" not in codigo,
      "a funcao nunca levanta (best-effort por desenho)")
check("httpx" in codigo and "X-API-Key" in codigo,
      "a ponte fala HTTP com o Conversas, autenticada")


# ============ 8. a rota generica nao e um segundo caminho ============
# AUDIT-2026-08-WF2 (W2-21) — `PUT /api/leads/{id}` aceitava `responsavel_id`
# no corpo e escrevia via setattr, sem gravar `responsavel_changed` no
# LeadHistory e sem chamar a ponte. O lead trocava de dono e a conversa
# continuava com a Bia ligada, fora da fila: o defeito principal desta rodada
# de volta, por uma porta lateral.
print()
print("8 — PUT /api/leads/{id} recusa trocar responsavel")

antes = responsavel_atual()
chamadas.clear()
r = client.put(f"/api/leads/{LEAD_ID}", json={"responsavel_id": JULIA_ID})
check(r.status_code == 422,
      f"corpo com responsavel_id -> 422 (obteve {r.status_code})")
check("responsavel" in r.json().get("detail", "").lower(),
      f"o 422 diz qual e a rota certa (obteve {r.json().get('detail')!r})")
check(responsavel_atual() == antes,
      f"o responsavel NAO mudou (era {antes}, esta {responsavel_atual()})")
check(chamadas == [],
      f"e a ponte nao foi chamada por este caminho (obteve {len(chamadas)})")

# A recusa e so do responsavel: os outros campos continuam atualizaveis pela
# mesma rota, senao a `Tool Atualizar Lead` do n8n quebraria inteira.
r = client.put(f"/api/leads/{LEAD_ID}", json={"nome": "Nome Trocado W221"})
check(r.status_code == 200,
      f"atualizar outro campo continua funcionando (obteve {r.status_code})")
check(r.json().get("nome") == "Nome Trocado W221",
      f"e o campo mudou de verdade (obteve {r.json().get('nome')!r})")


print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS TESTES DA PONTE DE HANDOFF PASSARAM")
