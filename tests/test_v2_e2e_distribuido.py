# -*- coding: utf-8 -*-
"""
BIA-V2 — E2E DISTRIBUIDO (fechamento de R-B / R15 / R16 / R17).

Fonte normativa: `bia-v2-r15-r17-design-v4.md`, secoes 3 (os Casos 1-6 + 2b),
4/4.2 (R15 e a particao de fila), 5 (R16), 8 (transacao/schema) e 8.5 (a
definicao dos cenarios A-M e o criterio global).

O QUE ESTE ARQUIVO E — E COMO ELE DIFERE DO E2E ANTERIOR
--------------------------------------------------------
`tests/test_v2_e2e_bia.py` (Fase 10) roda TUDO num processo so, contra SQLite,
com o CRM inteiro SUBSTITUIDO por um fake (`handoff_mod.httpx` trocado). Ele
prova a pilha do Conversas; nao prova nada sobre o CRM nem sobre a fronteira
entre os dois.

Aqui a montagem e a oposta, e e o ponto do arquivo:

  * PostgreSQL 16 REAL (efemero), com o schema criado pelo `create_all()` de
    cada app no proprio `lifespan` — nao ha migration `mNNN` a rodar a mao:
    banco novo nasce completo (`migrations/CLAUDE.md`).
  * CRM REAL, `app.main:app` servido por `uvicorn` de verdade, num PROCESSO
    proprio, ouvindo uma porta TCP real em 127.0.0.1.
  * Conversas REAL, `app.main:app` do lado Conversas, servido por `uvicorn` de
    verdade, em OUTRO processo, em OUTRA porta real.
  * HTTP GENUINO entre os dois: `CRM_BASE_URL` do Conversas aponta para a porta
    real do CRM, e `handoff._chamar_crm` abre um `httpx.Client` que trafega pela
    pilha TCP do sistema. NAO ha `ASGITransport`, `TestClient` nem qualquer
    mock de transporte em lugar nenhum deste arquivo — o round-trip e de rede.
  * `app/routers/v2_lead_domain.py`, `conversas/app/v2/handoff.py` e os dois
    workers (R15/R16) rodam SEM monkeypatch (a UNICA excecao esta na secao do
    teste negativo obrigatorio da secao 2.3 do desenho, que existe justamente
    para trocar o idioma de leitura sob lock e provar que a forma errada falha).

DOIS PROCESSOS PORQUE O PACOTE `app` COLIDE
-------------------------------------------
O CRM e o Conversas declaram, os dois, um pacote chamado `app`. Importar os
dois no MESMO processo Python e impossivel (e a razao documentada de a suite
rodar um processo por arquivo, `tests/CLAUDE.md`). Entao: cada app sobe no seu
processo, com seu `sys.path`, e este processo de teste importa APENAS o lado
Conversas (para rodar os workers R15/R16 de verdade) e fala com o CRM por SQL
cru e por HTTP.

O interpretador de cada subprocesso e configuravel:
`E2E_DIST_PYTHON_CRM` e `E2E_DIST_PYTHON_CONVERSAS` (default: o interpretador
deste processo). No CI cada um aponta para um virtualenv proprio — 3.11 com
`requirements.txt` para o CRM, 3.12 com `conversas/requirements.txt` para o
Conversas —, que e a MESMA separacao de ambiente que producao usa e que o
`CLAUDE.md` deste repo exige ("em ambiente SEPARADO; os pins conflitam").

O QUE E FAKE — E SO ISTO
------------------------
  1. Gemini/LLM. `interpretador._chamar_llm` e `comunicador._chamar_gemini` sao
     substituidos por fakes dentro do processo do Conversas (mesma fronteira que
     TODO o resto da suite ja substitui). Nenhuma chamada sai para o Google.
  2. `google.genai` como MODULO, e SO quando o pacote nao esta instalado no
     ambiente. Isto e IMPOSTO PELO AMBIENTE, nao uma escolha de desenho:
     `conversas/app/v2/comunicador.py` e `interpretador.py` fazem
     `from google import genai` / `from google.genai import types` no topo, e
     `google-genai` vive so em `conversas/requirements.txt` — um ambiente que
     tenha os pins do CRM (ou o ambiente de dev deste repo, onde so
     `google-generativeai` esta instalado) nao consegue nem IMPORTAR
     `conversas.app.main` sem o stub. O stub e CONDICIONAL de proposito: onde o
     pacote existe (o venv do Conversas no CI), o modulo real e importado e so
     as duas funcoes de fronteira sao substituidas.
  3. Meta/WhatsApp Cloud API: nenhuma chamada real (nenhum cenario aqui envia
     mensagem; `META_APP_SECRET` fica vazio).
  4. n8n: nenhuma chamada real (`N8N_AGENT_ENABLED=false`).

Nada mais e fake. Em particular NAO sao fake: o banco, o schema, os dois apps,
o transporte HTTP entre eles, o endpoint de handoff do CRM, o CAS sob lock de
linha, os dois workers, a maquina de estados local e a fila de `handoff_operations`.

HARNESS DE CONTROLE (`/__teste__/*`) — O QUE ELE E E O QUE ELE NAO E
--------------------------------------------------------------------
Os dois apps sobem por um lancador proprio deste teste (escrito em `scratch/`)
que instala um middleware ASGI respondendo o prefixo `/__teste__/`. Ele NAO
existe em producao e NAO altera nenhuma decisao de negocio. Serve para tres
coisas que um processo separado nao consegue fazer de fora:

  * CONFIGURAR o que ja e configuravel por ambiente — `V2_MODE`,
    `ATENDENTES_ELEGIVEIS`, o timeout de leitura de `_chamar_crm`. Sao os
    MESMOS valores que uma variavel de ambiente definiria; o codigo de producao
    continua lendo cada um exatamente onde ja lia.
  * PROGRAMAR o fake de LLM (qual interpretacao devolver, qual texto o
    comunicador produz, e o gancho do Cenario D).
  * ATRASAR ou RECUSAR a resposta do CRM de forma deterministica. O atraso e
    aplicado DEPOIS de `call_next` — ou seja, depois que a rota ja rodou e
    COMITOU: e assim que os Cenarios K e M produzem um timeout AMBIGUO de
    verdade (o CRM aplicou, o cliente desistiu) em vez de simula-lo. A recusa
    (`status forcado`) devolve antes de chamar a rota, entao o CRM comprovadamente
    NAO aplica — e o que monta a linha `FAILED` retriavel do Cenario G2.

POR QUE ESTE ARQUIVO NAO FAZ SKIP GRACIOSO
-------------------------------------------
Mesma regra de `tests/test_v2_rb_cas_postgres.py` (desenho, secoes 2.3 e 8): o
dialeto SQLite do SQLAlchemy DESCARTA `FOR UPDATE` em silencio, e a forma
errada do idioma passa em qualquer cenario nao concorrente. Um gate que pula
quando falta banco reporta verde sobre nada. Sem `DATABASE_URL_E2E_DIST`
alcancavel, este arquivo FALHA (exit 1). O CI provisiona o servico.

AMBIGUIDADES RESOLVIDAS AQUI, EXPLICITAMENTE (nao decididas em silencio)
------------------------------------------------------------------------
1. UM banco compartilhado, nao dois. O pedido original falava em "1 PostgreSQL
   para o CRM, 1 para o Conversas, bancos distintos", mandando confirmar o
   `docker-compose.yml` e REPLICAR. O compose diz o contrario da premissa:
   `docker-compose.yml:59` (CRM) e `:165` (Conversas) apontam para a MESMA
   `DATABASE_URL`, o mesmo banco `crm_atacama`, no mesmo servico `postgres` —
   nao ha "banco proprio de cada um". E a premissa nao e cosmetica:
   `handoff._ler_baseline_cas` e `worker_reconciliacao._dono_no_crm` leem
   `leads`/`users` do banco do CRM pela SESSAO DO CONVERSAS
   (`crm.get_leads_responsaveis`, "Both CRM and Conversas share the same
   PostgreSQL database in production"). Com bancos separados essa leitura
   devolveria `None`, o baseline do CAS ficaria AUSENTE e os Cenarios I/J/L/M
   passariam a testar um CAS inerte — verde sobre nada. Este teste replica
   producao: um servico PostgreSQL, um banco, tabelas de cada dono. O residual
   dessa premissa esta nomeado no desenho (secao 10, item 6).
2. As DUAS listas "A-H" que existem. `test_v2_e2e_bia.py` define A-G (A=triagem
   incompleta ... G=dry_run) e o desenho v3 (secao 7) define outra lista, onde
   G e "retomada concorrente + mensagem nova" (o cenario R-B que era ESPERADO
   VERMELHO) e H e "falha local permanente pos-CRM". A instrucao desta tarefa
   cita as duas ao mesmo tempo. Resolucao: A-F seguem `test_v2_e2e_bia.py` sem
   mudanca semantica; o G do desenho vira G2 (agora ESPERADO VERDE, secao 8.5)
   e o G do Fase 10 (dry_run) vira G1 — nenhuma cobertura e perdida; H e o do
   desenho. Os rotulos aparecem em cada bloco.
3. Teste negativo (sem `populate_existing()`): `tests/test_v2_rb_cas_postgres.py`
   ja o cobre em nivel isolado (TestClient in-process, 2 threads). O que SO este
   nivel prova e que a deteccao sobrevive ao round-trip HTTP real contra um
   `uvicorn` de verdade — isto e, que o resultado la nao era artefato da
   serializacao do `TestClient`. Por isso a secao final aqui NAO duplica os
   grupos 1/3 daquele arquivo: roda so o experimento controlado (mesma
   pre-carga, mesma barreira, unica variavel = `populate_existing()`) por cima
   de HTTP real.

COMO RODAR (PostgreSQL efemero, descartavel)
--------------------------------------------
    docker run -d --name bia_v2_e2e_dist_pg -p 55436:5432 \\
        -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \\
        -e POSTGRES_DB=e2edist postgres:16-alpine

    # PowerShell
    $env:DATABASE_URL_E2E_DIST = "postgresql://e2e:e2e@localhost:55436/e2edist"
    python tests/test_v2_e2e_distribuido.py

    docker stop bia_v2_e2e_dist_pg; docker rm bia_v2_e2e_dist_pg

ATENCAO: o banco apontado por `DATABASE_URL_E2E_DIST` e tratado como
DESCARTAVEL — o schema `public` inteiro e DROPADO e recriado a cada execucao.
Nunca aponte esta var para o Postgres de dev compartilhado nem para producao.

Este arquivo tem job PROPRIO no CI (`e2e-distribuido`), fora dos dois jobs
comuns: precisa dos DOIS ambientes ao mesmo tempo, e nenhum dos dois jobs os
tem. `tests/test_ci_job_routing_guard.py` guarda as duas metades (a exclusao e
o job), pelo mesmo motivo do gate do R-B.
"""
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Marcador de roteamento de job do CI e caminho real do segundo app. Usado como
# CODIGO (nao so em prosa) — ver tests/test_ci_job_routing_guard.py.
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

URL_BANCO = os.getenv("DATABASE_URL_E2E_DIST", "").strip()

if not URL_BANCO:
    print(
        "FAIL tests/test_v2_e2e_distribuido.py - `DATABASE_URL_E2E_DIST` ausente.\n"
        "     Este gate NAO faz skip gracioso, de proposito (ver o docstring do topo\n"
        "     e o desenho BIA-V2, secoes 2.3 e 8): em SQLite o SQLAlchemy DESCARTA\n"
        "     `FOR UPDATE` em silencio, entao um skip aqui viraria um gate inerte.\n"
        "     Suba o PostgreSQL efemero das instrucoes do docstring e rode de novo."
    )
    sys.exit(1)

if not URL_BANCO.startswith("postgresql"):
    print(
        f"FAIL DATABASE_URL_E2E_DIST precisa ser uma URL PostgreSQL (recebido: {URL_BANCO!r}).\n"
        "     R-B e sobre concorrencia com lock de linha real; rodar isto em SQLite\n"
        "     nao prova nada (e exatamente o ponto do risco)."
    )
    sys.exit(1)

PYTHON_CRM = os.getenv("E2E_DIST_PYTHON_CRM", "").strip() or sys.executable
PYTHON_CONVERSAS = os.getenv("E2E_DIST_PYTHON_CONVERSAS", "").strip() or sys.executable

# ── Identidades FIXAS ──────────────────────────────────────────────────────
# Ids escolhidos a mao (e nao gerados pela sequence) porque o subprocesso do
# Conversas precisa receber `ATENDENTES_ELEGIVEIS` no ambiente ANTES de subir,
# e o do CRM precisa de `V2_LEAD_DOMAIN_SERVICE_USER_ID` — nenhum dos dois pode
# esperar um id descoberto depois. Com ids fixos os dois apps sobem ja com a
# configuracao REAL (variavel de ambiente), sem monkeypatch de `app.config`.
ID_INTEGRADOR = 9001   # dono da X-API-Key; e o unico principal que o CRM aceita
ID_ANA = 9002
ID_BRUNO = 9003
ID_CARLA = 9004
ID_HELENA = 9005       # humana que assume a conversa no Cenario H
ID_INEXISTENTE = 999999

API_KEY = "bna_chave-do-e2e-distribuido"
WEBHOOK_AUTH = "segredo-e2e-distribuido"

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


def porta_livre():
    """Porta efemera de verdade: o SO escolhe, a gente le e devolve."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORTA_CRM = porta_livre()
PORTA_CONVERSAS = porta_livre()
BASE_CRM = f"http://127.0.0.1:{PORTA_CRM}"
BASE_CONVERSAS = f"http://127.0.0.1:{PORTA_CONVERSAS}"

# O ambiente PRECISA estar montado antes de qualquer import do pacote `app` do
# Conversas: `app/config.py` le tudo no import, e `handoff.py` congela
# `CRM_BASE_URL`/`BIA_V2_CRM_API_KEY` no topo do modulo.
os.environ["DATABASE_URL"] = URL_BANCO
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-e2e-distribuido"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ["GEMINI_API_KEY"] = ""
os.environ["CRM_BASE_URL"] = BASE_CRM
os.environ["BIA_V2_CRM_API_KEY"] = API_KEY
os.environ["V2_AGENT_WEBHOOK_AUTH_VALUE"] = WEBHOOK_AUTH
os.environ["V2_MODE"] = "live"

sys.path.insert(0, str(CONVERSAS_DIR))

# `google.genai` so quando o pacote real nao existe — ver item 2 do docstring.
try:  # noqa: SIM105
    from google import genai as _genai_instalado  # noqa: F401
except Exception:  # pragma: no cover - depende do ambiente, nao do codigo
    import types as _types

    try:
        import google as _google
    except Exception:
        _google = _types.ModuleType("google")
        sys.modules["google"] = _google
    _stub = _types.ModuleType("google.genai")
    _stub.Client = lambda **_kw: None
    _stub_types = _types.ModuleType("google.genai.types")
    _stub_types.GenerateContentConfig = lambda **_kw: None
    _stub.types = _stub_types
    _google.genai = _stub
    sys.modules["google.genai"] = _stub
    sys.modules["google.genai.types"] = _stub_types

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mesmo motivo documentado em tests/test_v2_e2e_bia.py).
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
from app.database import SessionLocal  # noqa: E402
from app.v2 import worker_fila, worker_reconciliacao, worker_retomada  # noqa: E402
from app.v2.eventos_validacao import OrigemEvento, TipoEvento  # noqa: E402

# Engine SO deste processo de teste, em AUTOCOMMIT: toda leitura de verificacao
# enxerga o ultimo estado COMITADO pelos outros dois processos, sem carregar
# snapshot de transacao nem identity map.
espiao = create_engine(URL_BANCO, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def sql(consulta, **params):
    with espiao.connect() as conn:
        return conn.execute(text(consulta), params).fetchall()


def sql_um(consulta, **params):
    linhas = sql(consulta, **params)
    return linhas[0] if linhas else None


def sql_valor(consulta, **params):
    linha = sql_um(consulta, **params)
    return linha[0] if linha is not None else None


def executar(consulta, **params):
    with espiao.connect() as conn:
        conn.execute(text(consulta), params)


# ═══════════════════════════════════════════════════════════════════════════
# LANCADORES — os dois apps REAIS, cada um no seu processo
# ═══════════════════════════════════════════════════════════════════════════
# Ambos instalam o middleware do harness (`/__teste__/*`) em vez de rotas: o
# middleware roda ANTES do roteamento, entao nao ha risco de perder para uma
# rota catch-all de `pages.py` registrada antes dele.

_LANCADOR_CRM = r'''
import asyncio
import json
import os
import sys
import threading

sys.path.insert(0, os.environ["E2E_DIST_ROOT"])

import uvicorn
from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.main import app
from app.models.lead import Lead
from app.routers import v2_lead_domain as v2_mod

CAMINHO_HANDOFF = "/api/v2/lead-domain/handoff"

_controles = {
    "atraso_ms": 0, "atraso_restantes": 0, "status": 0, "status_restantes": 0,
    "atraso_em_andamento": False,
}

_carregar_real = v2_mod._carregar_lead_sob_lock
_barreira = threading.Barrier(2)
_identidades = []


def _com_barreira(quebrada):
    """Reproduz, sobre HTTP real, o experimento controlado da secao 2.3.

    A PRE-CARGA existe nas duas variantes de proposito: e ela que deixa o `Lead`
    no identity map do SQLAlchemy, condicao sob a qual a forma sem
    `populate_existing()` devolve o objeto OBSOLETO mesmo tendo adquirido o lock
    de verdade. A referencia precisa ficar VIVA (o identity map guarda
    referencia fraca) — nao troque `precarga` por uma chamada solta.

    A barreira dispara DEPOIS da pre-carga e ANTES da leitura sob lock: garante
    que os dois lados ja carregaram o snapshot obsoleto antes de disputar o lock.
    """

    def _substituto(db, lead_id):
        precarga = db.query(Lead).filter(Lead.id == lead_id).first()
        try:
            _barreira.wait(timeout=30)
        except threading.BrokenBarrierError:
            _barreira.reset()
        if quebrada:
            # A forma NATURAL e ERRADA: "so adicionar FOR UPDATE na query que ja
            # existe". O lock de linha E adquirido no PostgreSQL, mas o
            # SQLAlchemy devolve o objeto ja em memoria, com `responsavel_id`
            # obsoleto — e o compare-and-swap compara contra lixo.
            lead = db.query(Lead).filter(Lead.id == lead_id).with_for_update().first()
        else:
            # A forma real do modulo, sem copia: se `_carregar_lead_sob_lock`
            # mudar, este braco do experimento muda junto.
            lead = _carregar_real(db, lead_id)
        _identidades.append(lead is precarga)
        if lead is None:
            raise HTTPException(
                status_code=404,
                detail={"error": v2_mod.TokenErroHandoff.LEAD_NOT_FOUND},
            )
        return lead

    return _substituto


async def _harness(request):
    caminho = request.url.path
    if caminho == "/__teste__/ping":
        return JSONResponse({"ok": True})
    if caminho == "/__teste__/variante-lock" and request.method == "GET":
        return JSONResponse({"identidades": list(_identidades)})
    if caminho == "/__teste__/atraso" and request.method == "GET":
        # Sinal observavel do Cenario H: True do momento em que o middleware
        # comecou a represar a resposta (depois de `call_next`, rota ja
        # comitada) ate o fim do `asyncio.sleep` — ver abaixo.
        return JSONResponse({"em_andamento": _controles["atraso_em_andamento"]})
    corpo = json.loads((await request.body()) or b"{}")
    if caminho == "/__teste__/atraso":
        _controles["atraso_ms"] = int(corpo.get("ms", 0))
        _controles["atraso_restantes"] = int(corpo.get("requisicoes", 0))
        return JSONResponse({"ok": True})
    if caminho == "/__teste__/status":
        _controles["status"] = int(corpo.get("codigo", 0))
        _controles["status_restantes"] = int(corpo.get("requisicoes", 0))
        return JSONResponse({"ok": True})
    if caminho == "/__teste__/variante-lock":
        modo = corpo.get("modo", "producao")
        _identidades.clear()
        _barreira.reset()
        if modo == "producao":
            v2_mod._carregar_lead_sob_lock = _carregar_real
        elif modo == "barreira_correta":
            v2_mod._carregar_lead_sob_lock = _com_barreira(False)
        elif modo == "barreira_quebrada":
            v2_mod._carregar_lead_sob_lock = _com_barreira(True)
        else:
            return JSONResponse({"erro": "modo desconhecido"}, status_code=400)
        return JSONResponse({"ok": True, "modo": modo})
    return JSONResponse({"erro": "rota de harness desconhecida"}, status_code=404)


@app.middleware("http")
async def _middleware_harness(request, call_next):
    if request.url.path.startswith("/__teste__/"):
        return await _harness(request)
    if request.url.path == CAMINHO_HANDOFF and _controles["status_restantes"] > 0:
        # Recusa ANTES da rota: o CRM comprovadamente NAO aplica nada.
        _controles["status_restantes"] -= 1
        return JSONResponse(
            {"detail": {"error": "HARNESS_STATUS_FORCADO"}}, status_code=_controles["status"]
        )
    resposta = await call_next(request)
    if request.url.path == CAMINHO_HANDOFF and _controles["atraso_restantes"] > 0:
        # Atraso DEPOIS de `call_next`: a rota ja rodou e ja COMITOU. E assim
        # que se produz um timeout AMBIGUO de verdade (o CRM aplicou, o cliente
        # desistiu), em vez de simular um.
        _controles["atraso_restantes"] -= 1
        _controles["atraso_em_andamento"] = True
        await asyncio.sleep(_controles["atraso_ms"] / 1000.0)
        _controles["atraso_em_andamento"] = False
    return resposta


uvicorn.run(app, host="127.0.0.1", port=int(os.environ["E2E_DIST_PORTA"]), log_level="warning")
'''


_LANCADOR_CONVERSAS = r'''
import json
import os
import sys
import types

sys.path.insert(0, os.environ["E2E_DIST_CONVERSAS_DIR"])

# `google.genai` SO quando o pacote real nao existe — imposicao de ambiente,
# nunca escolha de desenho (ver o docstring do teste).
try:
    from google import genai as _instalado  # noqa: F401
except Exception:
    try:
        import google as _google
    except Exception:
        _google = types.ModuleType("google")
        sys.modules["google"] = _google
    _stub = types.ModuleType("google.genai")
    _stub.Client = lambda **_kw: None
    _stub_types = types.ModuleType("google.genai.types")
    _stub_types.GenerateContentConfig = lambda **_kw: None
    _stub.types = _stub_types
    _google.genai = _stub
    sys.modules["google.genai"] = _stub
    sys.modules["google.genai.types"] = _stub_types

import httpx
import uvicorn
from sqlalchemy import text
from starlette.responses import JSONResponse

import app.v2.comunicador as comunicador_mod
import app.v2.handoff as handoff_mod
import app.v2.interpretador as interpretador_mod
import app.v2.pipeline as pipeline_mod
from app.database import SessionLocal
from app.main import app

_ia = {
    "explicit_human_request": False,
    "extracted": {},
    "duvidas": [],
    "comunicador_modo": "fixo",
    "comunicador_resposta": "Oi! Recebi sua mensagem.",
    "gancho": None,
    "interpretador_chamadas": 0,
    "comunicador_chamadas": 0,
    "comunicador_ultimo_prompt": None,
}


def _fake_chamar_llm(mensagem):
    _ia["interpretador_chamadas"] += 1
    gancho = _ia["gancho"]
    if gancho:
        # Segundo writer DENTRO da fronteira da IA (Cenario D): sessao propria,
        # transacao propria, exatamente como um humano assumindo a conversa
        # enquanto a IA "pensa".
        sessao = SessionLocal()
        try:
            sessao.execute(
                text(
                    "UPDATE conversations SET state_version = :v, atendente_id = :a "
                    "WHERE id = :c"
                ),
                {"v": gancho["state_version"], "a": gancho["atendente_id"],
                 "c": gancho["conversation_id"]},
            )
            sessao.commit()
        finally:
            sessao.close()
    return json.dumps({
        "intent": None,
        "explicit_human_request": _ia["explicit_human_request"],
        "extracted": _ia["extracted"],
        "duvidas": _ia["duvidas"],
    })


def _fake_chamar_gemini(prompt):
    _ia["comunicador_chamadas"] += 1
    _ia["comunicador_ultimo_prompt"] = prompt
    if _ia["comunicador_modo"] == "eco":
        return prompt
    return _ia["comunicador_resposta"]


interpretador_mod._chamar_llm = _fake_chamar_llm
comunicador_mod._chamar_gemini = _fake_chamar_gemini


async def _harness(request):
    caminho = request.url.path
    if caminho == "/__teste__/ping":
        return JSONResponse({"ok": True})
    if caminho == "/__teste__/ia" and request.method == "GET":
        return JSONResponse({
            "interpretador_chamadas": _ia["interpretador_chamadas"],
            "comunicador_chamadas": _ia["comunicador_chamadas"],
            "comunicador_ultimo_prompt": _ia["comunicador_ultimo_prompt"],
        })
    corpo = json.loads((await request.body()) or b"{}")
    if caminho == "/__teste__/ia":
        _ia["explicit_human_request"] = bool(corpo.get("explicit_human_request", False))
        _ia["extracted"] = corpo.get("extracted") or {}
        _ia["duvidas"] = corpo.get("duvidas") or []
        _ia["comunicador_modo"] = corpo.get("comunicador_modo", "fixo")
        _ia["comunicador_resposta"] = corpo.get(
            "comunicador_resposta", "Oi! Recebi sua mensagem."
        )
        _ia["gancho"] = corpo.get("gancho")
        _ia["interpretador_chamadas"] = 0
        _ia["comunicador_chamadas"] = 0
        _ia["comunicador_ultimo_prompt"] = None
        return JSONResponse({"ok": True})
    if caminho == "/__teste__/config":
        # Os TRES sao configuracao, nao substituicao de comportamento: os mesmos
        # valores que uma variavel de ambiente definiria, lidos pelo codigo de
        # producao exatamente onde ele ja os lia.
        if "v2_mode" in corpo:
            pipeline_mod.V2_MODE = corpo["v2_mode"]
        if "atendentes_elegiveis" in corpo:
            os.environ["ATENDENTES_ELEGIVEIS"] = str(corpo["atendentes_elegiveis"])
        if "timeout_crm_read" in corpo:
            handoff_mod._TIMEOUT_CRM = httpx.Timeout(
                float(corpo["timeout_crm_read"]), connect=5.0
            )
        return JSONResponse({
            "ok": True,
            "v2_mode": pipeline_mod.V2_MODE,
            "atendentes_elegiveis": os.environ.get("ATENDENTES_ELEGIVEIS", ""),
        })
    return JSONResponse({"erro": "rota de harness desconhecida"}, status_code=404)


@app.middleware("http")
async def _middleware_harness(request, call_next):
    if request.url.path.startswith("/__teste__/"):
        return await _harness(request)
    return await call_next(request)


uvicorn.run(app, host="127.0.0.1", port=int(os.environ["E2E_DIST_PORTA"]), log_level="warning")
'''


# ═══════════════════════════════════════════════════════════════════════════
# CICLO DE VIDA DOS DOIS PROCESSOS
# ═══════════════════════════════════════════════════════════════════════════
processos = []
logs = {}


def subir(nome, script, interpretador, porta, extra_env):
    caminho = SCRATCH / f"e2e_dist_{nome}.py"
    caminho.write_text(script, encoding="utf-8")
    log = SCRATCH / f"e2e_dist_{nome}.log"
    ambiente = dict(os.environ)
    ambiente["E2E_DIST_PORTA"] = str(porta)
    ambiente["E2E_DIST_ROOT"] = str(ROOT)
    ambiente["E2E_DIST_CONVERSAS_DIR"] = str(CONVERSAS_DIR)
    ambiente.update(extra_env)
    arquivo = open(log, "w", encoding="utf-8", errors="replace")
    processo = subprocess.Popen(
        [interpretador, str(caminho)], stdout=arquivo, stderr=subprocess.STDOUT,
        env=ambiente, cwd=str(ROOT),
    )
    processos.append((nome, processo, arquivo))
    logs[nome] = log
    return processo


def esperar_de_pe(nome, base, processo, limite_s=90):
    fim = time.time() + limite_s
    while time.time() < fim:
        if processo.poll() is not None:
            despejar_logs()
            raise SystemExit(
                f"FAIL o processo `{nome}` morreu antes de responder "
                f"(exit={processo.returncode}); veja {logs[nome]}"
            )
        try:
            if httpx.get(f"{base}/__teste__/ping", timeout=2.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.25)
    despejar_logs()
    raise SystemExit(f"FAIL o processo `{nome}` nao respondeu em {limite_s}s; veja {logs[nome]}")


def despejar_logs(linhas=40):
    for nome, caminho in logs.items():
        try:
            texto = caminho.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        print(f"\n----- ultimas {linhas} linhas de {nome} ({caminho}) -----")
        for linha in texto[-linhas:]:
            print(f"  | {linha}")


def derrubar():
    for nome, processo, arquivo in processos:
        if processo.poll() is None:
            processo.terminate()
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                processo.kill()
                processo.wait(timeout=10)
        try:
            arquivo.close()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# CLIENTES HTTP REAIS
# ═══════════════════════════════════════════════════════════════════════════
def crm_handoff(*, event_id, lead_id, target_user_id, expected=None, mandar_expected=True,
                timeout=30.0):
    """POST HTTP REAL no endpoint de handoff do CRM. Sem transporte fake."""
    corpo = {"event_id": event_id, "lead_id": lead_id, "target_user_id": target_user_id}
    if mandar_expected:
        # Campo AUSENTE x `null` explicito e a distincao da secao 2.1.2: sem a
        # chave o CRM aplica incondicional (chamador pre-R-B); com `null` o CAS
        # exige que o lead esteja SEM dono.
        corpo["expected_responsavel_id"] = expected
    return httpx.post(
        f"{BASE_CRM}/api/v2/lead-domain/handoff", json=corpo,
        headers={"X-API-Key": API_KEY}, timeout=timeout,
    )


def crm_release(lead_id):
    """Release LEGITIMO pela rota V1 REAL (`PUT /api/leads/{id}/responsavel`).

    Sem `responsavel_id` na query o lead volta para o "Agente IA" (`None`) — e
    o Caso 6 do desenho. E, de proposito, um writer V1: ele nao passa pelo CAS
    nem grava no livro-razao, exatamente como em producao (residual R10).
    """
    return httpx.put(
        f"{BASE_CRM}/api/leads/{lead_id}/responsavel",
        headers={"X-API-Key": API_KEY}, timeout=30.0,
    )


def crm_controle(rota, **corpo):
    return httpx.post(f"{BASE_CRM}/__teste__/{rota}", json=corpo, timeout=30.0).json()


def crm_identidades():
    return httpx.get(f"{BASE_CRM}/__teste__/variante-lock", timeout=30.0).json()["identidades"]


def esperar_atraso_iniciado(limite_s=30.0, intervalo_s=0.05):
    """Poll ate o middleware do CRM comecar a represar a resposta do handoff.

    Mesmo padrao de `esperar_de_pe`: nunca um `time.sleep` as cegas torcendo
    para o timing dar certo. O sinal (`_controles["atraso_em_andamento"]` no
    processo do CRM) so fica True DEPOIS de `call_next` — ou seja, a rota do
    CRM ja rodou e ja COMITOU — e ANTES do `asyncio.sleep` que represa a
    resposta. Usado pelo Cenario H para abrir, de forma deterministica, a
    janela em que um humano real pode assumir a conversa enquanto a resposta
    do CRM ainda esta represada.
    """
    fim = time.time() + limite_s
    while time.time() < fim:
        if httpx.get(f"{BASE_CRM}/__teste__/atraso", timeout=2.0).json()["em_andamento"]:
            return
        time.sleep(intervalo_s)
    raise SystemExit(
        "FAIL o atraso do CRM (Cenario H) nunca comecou a represar a resposta "
        f"em {limite_s}s"
    )


def configurar_ia(*, explicit_human_request=False, extracted=None, duvidas=None,
                  comunicador_modo="fixo", comunicador_resposta="Oi! Recebi sua mensagem.",
                  gancho=None):
    httpx.post(f"{BASE_CONVERSAS}/__teste__/ia", timeout=30.0, json={
        "explicit_human_request": explicit_human_request,
        "extracted": extracted or {},
        "duvidas": duvidas or [],
        "comunicador_modo": comunicador_modo,
        "comunicador_resposta": comunicador_resposta,
        "gancho": gancho,
    }).raise_for_status()


def estado_ia():
    return httpx.get(f"{BASE_CONVERSAS}/__teste__/ia", timeout=30.0).json()


def configurar_conversas(**corpo):
    return httpx.post(f"{BASE_CONVERSAS}/__teste__/config", json=corpo, timeout=30.0).json()


def webhook(conversation_id, *, mensagem="Ola.", event_id=None, timeout=60.0):
    """POST HTTP REAL em `/webhook/agent-bia-v2` do Conversas."""
    corpo = {
        "conversation_id": conversation_id,
        "origem": OrigemEvento.SINTETICO.value,
        "lote": [{"msg_type": "text", "content": mensagem}],
        "mensagem_interpretacao": mensagem,
    }
    if event_id is not None:
        corpo["event_id"] = event_id
    return httpx.post(
        f"{BASE_CONVERSAS}/webhook/agent-bia-v2", json=corpo,
        headers={"X-Webhook-Auth": WEBHOOK_AUTH}, timeout=timeout,
    )


# ═══════════════════════════════════════════════════════════════════════════
# LEITORES DE ESTADO (sempre do banco, sempre o ultimo comitado)
# ═══════════════════════════════════════════════════════════════════════════
_COLUNAS_CONVERSA = (
    "state", "state_version", "is_bot_active", "atendente_id", "responsavel_id",
    "queued_at", "primeira_resposta_humana_at", "status", "closed_at",
)


def snapshot_conversa(cid):
    linha = sql_um(
        f"SELECT {', '.join(_COLUNAS_CONVERSA)} FROM conversations WHERE id = :c", c=cid
    )
    return dict(zip(_COLUNAS_CONVERSA, linha)) if linha is not None else None


def trilha_tipos(cid):
    return [
        linha[0] for linha in
        sql("SELECT event_type FROM conversation_events WHERE conversation_id = :c "
            "ORDER BY id", c=cid)
    ]


def contar_eventos(cid, tipo):
    return sql_valor(
        "SELECT count(*) FROM conversation_events WHERE conversation_id = :c "
        "AND event_type = :t", c=cid, t=tipo.value,
    )


def error_code_do_evento(cid, tipo):
    return sql_valor(
        "SELECT error_code FROM conversation_events WHERE conversation_id = :c "
        "AND event_type = :t ORDER BY id DESC LIMIT 1", c=cid, t=tipo.value,
    )


def operacao(event_id):
    linha = sql_um(
        "SELECT id, status, error_code, error_class, target_user_id, attempt_count, "
        "reconciled_at, conversation_id FROM handoff_operations WHERE event_id = :e",
        e=event_id,
    )
    if linha is None:
        return None
    return dict(zip(
        ("id", "status", "error_code", "error_class", "target_user_id",
         "attempt_count", "reconciled_at", "conversation_id"), linha,
    ))


def dono_no_crm(lead_id):
    return sql_valor("SELECT responsavel_id FROM leads WHERE id = :l", l=lead_id)


def linhas_livro_razao(lead_id):
    return sql_valor(
        "SELECT count(*) FROM lead_handoff_operations WHERE lead_id = :l", l=lead_id
    )


def criar_lead(nome, whatsapp=None):
    with espiao.connect() as conn:
        return conn.execute(
            text("INSERT INTO leads (nome, whatsapp) VALUES (:n, :w) RETURNING id"),
            {"n": nome, "w": whatsapp or f"+5511{uuid.uuid4().int % 10**9:09d}"},
        ).scalar()


_contador = iter(range(1, 100000))


def criar_conversa(lead_id, *, state="AI_TRIAGE", state_version=0):
    n = next(_contador)
    with espiao.connect() as conn:
        return conn.execute(
            text(
                "INSERT INTO conversations (lead_id, whatsapp, status, unread_count, "
                "is_bot_active, state, state_version, last_customer_msg_at) "
                "VALUES (:l, :w, 'aberta', 0, true, :s, :v, now()) RETURNING id"
            ),
            {"l": lead_id, "w": f"5511901{n:06d}", "s": state, "v": state_version},
        ).scalar()


def criterio_global(rotulo, cid, lead_id):
    """O criterio do desenho, secao 8.5, valido para TODO cenario que termina em
    sucesso: `CRM.leads.responsavel_id == conversations.atendente_id ==
    conversations.responsavel_id`."""
    conv = snapshot_conversa(cid)
    dono = dono_no_crm(lead_id)
    check(
        dono is not None and dono == conv["atendente_id"] == conv["responsavel_id"],
        f"{rotulo}. CRITERIO GLOBAL: CRM.leads.responsavel_id={dono} == "
        f"conversations.atendente_id={conv['atendente_id']} == "
        f"conversations.responsavel_id={conv['responsavel_id']}",
    )


# Triagem completa, mesma forma ja provada em tests/test_v2_pipeline.py e
# reusada por tests/test_v2_e2e_bia.py.
TRIAGEM_COMPLETA = {
    "nome": "Maria", "email": "maria@x.com", "total_pessoas": 2, "adultos": 2,
    "criancas": 0, "datas_definidas": False,
    "destinos": [{"destino": "Atacama", "dias": 5}],
}
MSG_COMPLETA = ("Meu nome e Maria, meu email e maria@x.com, somos 2 adultos "
                "indo 5 dias pro Atacama.")


# ═══════════════════════════════════════════════════════════════════════════
# BOOTSTRAP
# ═══════════════════════════════════════════════════════════════════════════
def _hash_api_key(chave):
    """Mesma funcao de `app/auth.py::hash_api_key` (SHA-256 hex). Reescrita aqui
    porque o pacote `app` deste processo e o do CONVERSAS, nao o do CRM."""
    import hashlib

    return hashlib.sha256(chave.encode()).hexdigest()


print("=" * 74)
print("BIA-V2 — E2E DISTRIBUIDO (A-M) — dois apps reais, HTTP real, PostgreSQL real")
print("=" * 74)

# `DROP SCHEMA public CASCADE` em vez de `drop_all()`: nao depende da ordem de
# FK nem de o metadata conhecer o que ficou de uma execucao anterior.
executar("DROP SCHEMA IF EXISTS public CASCADE")
executar("CREATE SCHEMA public")

_versao = sql_valor("SHOW server_version")
print(f"PostgreSQL       : {_versao}")
print(f"CRM              : {BASE_CRM}  ({PYTHON_CRM})")
print(f"Conversas        : {BASE_CONVERSAS}  ({PYTHON_CONVERSAS})")
print(f"Banco            : {URL_BANCO}")
print("-" * 74)

try:
    # 1. CRM primeiro: e ele o DONO de `users` (o `create_all` do Conversas so
    #    cria um espelho aproximado — ver o lifespan de conversas/app/main.py).
    proc_crm = subir("crm", _LANCADOR_CRM, PYTHON_CRM, PORTA_CRM, {
        "SEED_INITIAL_ADMIN": "false",
        "V2_LEAD_DOMAIN_SERVICE_USER_ID": str(ID_INTEGRADOR),
    })
    esperar_de_pe("crm", BASE_CRM, proc_crm)
    print(f"CRM de pe em {BASE_CRM} (schema criado pelo lifespan real)")

    # 2. Usuarios com id FIXO, para que o Conversas ja suba com
    #    ATENDENTES_ELEGIVEIS certo e o CRM com o usuario de servico certo.
    for uid, nome, email, chave in (
        (ID_INTEGRADOR, "Integrador BIA", "integrador@e2edist.local", API_KEY),
        (ID_ANA, "Ana", "ana@e2edist.local", None),
        (ID_BRUNO, "Bruno", "bruno@e2edist.local", None),
        (ID_CARLA, "Carla", "carla@e2edist.local", None),
        (ID_HELENA, "Helena", "helena@e2edist.local", None),
    ):
        executar(
            # 'USER' entra como literal (tipo `unknown`, coagido para o enum
            # nativo `userrole`); um bind param viria tipado como text e o
            # PostgreSQL recusaria.
            "INSERT INTO users (id, nome, email, hashed_password, role, is_active, "
            "email_verified, api_key) VALUES (:i, :n, :e, 'x', 'USER', true, true, :k)",
            i=uid, n=nome, e=email, k=_hash_api_key(chave) if chave else None,
        )

    # 3. Conversas depois, ja com a configuracao real no AMBIENTE.
    proc_conv = subir("conversas", _LANCADOR_CONVERSAS, PYTHON_CONVERSAS, PORTA_CONVERSAS, {
        "ATENDENTES_ELEGIVEIS": str(ID_ANA),
        "V2_MODE": "live",
    })
    esperar_de_pe("conversas", BASE_CONVERSAS, proc_conv)
    print(f"Conversas de pe em {BASE_CONVERSAS} (CRM_BASE_URL={BASE_CRM})")

    _iso = sql_valor("SHOW transaction_isolation")
    print("-" * 74)
    check(_iso == "read committed",
          f"banco em READ COMMITTED — invariante do ramo de replay (veio {_iso!r})")
    check(
        httpx.get(f"{BASE_CRM}/__teste__/ping", timeout=5.0).status_code == 200
        and httpx.get(f"{BASE_CONVERSAS}/__teste__/ping", timeout=5.0).status_code == 200,
        "os dois apps respondem em portas TCP reais distintas (HTTP genuino, sem ASGITransport)",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO A — triagem incompleta -> pedido de campo faltante
    # ═══════════════════════════════════════════════════════════════════════
    # Semantica de tests/test_v2_e2e_bia.py, agora sobre infra real. Sem pedido
    # explicito e com triagem incompleta o pipeline nunca chega perto do
    # handoff: `comunicador_modo="eco"` devolve o PROPRIO prompt, entao os nomes
    # dos campos faltantes aparecerem na resposta HTTP prova que a lista
    # calculada pela triagem atravessou comunicador+guard sem se perder.
    print("\nA. Triagem incompleta -> nunca chega ao handoff")
    lead_a = criar_lead("Lead A")
    cid_a = criar_conversa(lead_a)
    snap_a_antes = snapshot_conversa(cid_a)
    configurar_ia(extracted={"nome": "Carlos", "destinos": [{"destino": "Atacama"}]},
                  comunicador_modo="eco")
    r = webhook(cid_a, mensagem="Oi, sou o Carlos e quero ir pro Atacama.")
    corpo = r.json()
    check(r.status_code == 200, f"A1. 200 de sucesso (veio {r.status_code}: {r.text[:160]})")
    check(corpo.get("etapa_final") == "resposta_gerada",
          f"A2. desfecho resposta_gerada — nao avancou para handoff (veio {corpo.get('etapa_final')})")
    _faltantes = ["email", "total_pessoas", "adultos", "criancas", "datas_definidas"]
    _texto = corpo.get("texto_resposta") or ""
    check("Acao a comunicar: solicitar_campos_faltantes" in _texto,
          "A3. a acao repassada ao comunicador foi solicitar_campos_faltantes")
    check(all(c in _texto for c in _faltantes),
          f"A4. a resposta final menciona TODOS os campos que faltam {_faltantes}")
    check(trilha_tipos(cid_a) == [
        TipoEvento.MESSAGE_RECEIVED.value, TipoEvento.AI_INTERPRETATION_STARTED.value,
        TipoEvento.AI_INTERPRETATION_COMPLETED.value, TipoEvento.TRIAGE_DATA_UPDATED.value,
        TipoEvento.AI_RESPONSE_GENERATED.value,
    ], f"A5. trilha sem NENHUM evento de handoff (veio {trilha_tipos(cid_a)})")
    check(snapshot_conversa(cid_a) == snap_a_antes, "A6. NENHUMA coluna de conversations mudou")
    check(dono_no_crm(lead_a) is None and linhas_livro_razao(lead_a) == 0,
          "A7. o CRM REAL nunca foi tocado — lead sem dono e livro-razao vazio")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO B — triagem completa -> handoff live -> WAITING_HUMAN
    # ═══════════════════════════════════════════════════════════════════════
    # A diferenca em relacao ao Cenario B do E2E anterior e inteira: la o CRM era
    # um fake HTTP em memoria; aqui a chamada sai pela rede, chega no `uvicorn`
    # do CRM, roda `aplicar_handoff` de verdade, toma lock de linha no PostgreSQL,
    # grava o livro-razao e o `lead_history`, e so entao volta.
    print("\nB. Triagem completa -> handoff REAL pela rede -> WAITING_HUMAN")
    lead_b = criar_lead("Lead B")
    cid_b = criar_conversa(lead_b)
    ev_b = str(uuid.uuid4())
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    r = webhook(cid_b, mensagem=MSG_COMPLETA, event_id=ev_b)
    corpo = r.json()
    check(r.status_code == 200, f"B1. 200 de sucesso (veio {r.status_code}: {r.text[:160]})")
    check(corpo.get("etapa_final") == "handoff_concluido"
          and corpo.get("atendente_id") == ID_ANA,
          f"B2. desfecho handoff_concluido com o atendente elegivel (veio {corpo})")
    snap_b = snapshot_conversa(cid_b)
    check(snap_b["state"] == "WAITING_HUMAN", f"B3. conversa em WAITING_HUMAN ({snap_b['state']!r})")
    check(snap_b["state_version"] == 1, f"B4. state_version incrementou 1x (={snap_b['state_version']})")
    check(snap_b["is_bot_active"] is False and snap_b["atendente_id"] == ID_ANA
          and snap_b["responsavel_id"] == ID_ANA and snap_b["queued_at"] is not None
          and snap_b["primeira_resposta_humana_at"] is None and snap_b["status"] == "aberta",
          f"B5. as sete invariantes de WAITING_HUMAN valem ({snap_b})")
    op_b = operacao(ev_b)
    check(op_b is not None and op_b["status"] == "COMPLETED" and op_b["target_user_id"] == ID_ANA,
          f"B6. handoff_operations COMPLETED com o atendente correto ({op_b})")
    check(dono_no_crm(lead_b) == ID_ANA,
          f"B7. o CRM REAL aplicou leads.responsavel_id={ID_ANA} (veio {dono_no_crm(lead_b)})")
    _reg_b = sql_um(
        "SELECT event_id, target_user_id, previous_responsavel_id FROM "
        "lead_handoff_operations WHERE lead_id = :l", l=lead_b,
    )
    check(linhas_livro_razao(lead_b) == 1 and _reg_b is not None
          and _reg_b[0] == ev_b and _reg_b[1] == ID_ANA and _reg_b[2] is None,
          f"B8. livro-razao do CRM com UMA linha, event_id igual e previous_responsavel_id NULL ({_reg_b})")
    check(sql_valor("SELECT count(*) FROM lead_history WHERE lead_id = :l AND "
                    "evento = 'responsavel_changed'", l=lead_b) == 1,
          "B9. exatamente 1 lead_history responsavel_changed, na mesma transacao do CRM")
    check(estado_ia()["comunicador_chamadas"] == 0,
          "B10. comunicador NUNCA chamado — handoff bem sucedido para antes do gate/comunicador")
    criterio_global("B11", cid_b, lead_b)

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO C — sem atendente elegivel -> permanece em AI_TRIAGE
    # ═══════════════════════════════════════════════════════════════════════
    # SEM monkeypatch de `selecionar_atendente` (o E2E anterior precisava de um,
    # porque nao tinha como configurar o processo): aqui a elegibilidade e
    # apontada para um id que nao existe, e `resolver_atendente_elegivel` devolve
    # None pelo caminho REAL — a mesma coisa que acontece em producao quando a
    # configuracao lista alguem desligado.
    print("\nC. Sem atendente elegivel -> conversa fica em AI_TRIAGE")
    configurar_conversas(atendentes_elegiveis=ID_INEXISTENTE)
    lead_c = criar_lead("Lead C")
    cid_c = criar_conversa(lead_c)
    snap_c_antes = snapshot_conversa(cid_c)
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    r = webhook(cid_c, mensagem=MSG_COMPLETA)
    corpo = r.json()
    check(r.status_code == 200, f"C1. 200 de sucesso (veio {r.status_code})")
    check(corpo.get("etapa_final") == "resposta_gerada",
          f"C2. sem atendente, cai para o comunicador em vez de travar (veio {corpo.get('etapa_final')})")
    check(snapshot_conversa(cid_c) == snap_c_antes,
          "C3. NENHUMA coluna de conversations mudou (nunca saiu de AI_TRIAGE)")
    check(contar_eventos(cid_c, TipoEvento.HANDOFF_FAILED) == 1
          and error_code_do_evento(cid_c, TipoEvento.HANDOFF_FAILED) == "SEM_ATENDENTE_ELEGIVEL",
          "C4. exatamente 1 HANDOFF_FAILED com error_code=SEM_ATENDENTE_ELEGIVEL")
    check(contar_eventos(cid_c, TipoEvento.HANDOFF_REQUESTED) == 0
          and contar_eventos(cid_c, TipoEvento.HANDOFF_COMPLETED) == 0,
          "C5. ZERO HANDOFF_REQUESTED/HANDOFF_COMPLETED — a selecao nunca chegou a valer")
    check(sql_valor("SELECT count(*) FROM handoff_operations WHERE conversation_id = :c",
                    c=cid_c) == 0,
          "C6. ZERO linha em handoff_operations para esta conversa")
    check(dono_no_crm(lead_c) is None and linhas_livro_razao(lead_c) == 0,
          "C7. o CRM REAL nunca foi chamado — lead sem dono, livro-razao vazio")
    configurar_conversas(atendentes_elegiveis=ID_ANA)

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO D — humano assume no meio do fluxo -> resposta descartada
    # ═══════════════════════════════════════════════════════════════════════
    # O segundo writer muda `state_version` E `atendente_id` por uma SESSAO
    # SEPARADA, DENTRO da fronteira do interpretador. O gate checa
    # `state_version` ANTES de `atendente_id`, e so a PRIMEIRA condicao que
    # falha decide o error_code.
    print("\nD. Humano assume enquanto a IA pensa -> gate descarta a resposta")
    lead_d = criar_lead("Lead D")
    cid_d = criar_conversa(lead_d)
    configurar_ia(gancho={"conversation_id": cid_d, "state_version": 1,
                          "atendente_id": ID_CARLA})
    r = webhook(cid_d)
    corpo = r.json()
    check(r.status_code == 200, f"D1. 200 (desfecho de negocio, nao erro) (veio {r.status_code})")
    check(corpo.get("etapa_final") == "resposta_descartada_gate"
          and corpo.get("texto_resposta") is None,
          f"D2. desfecho resposta_descartada_gate, sem texto saindo (veio {corpo})")
    check(estado_ia()["comunicador_chamadas"] == 0,
          "D3. comunicador NUNCA chamado — o gate recusou antes")
    check(contar_eventos(cid_d, TipoEvento.AI_RESPONSE_DISCARDED) == 1
          and error_code_do_evento(cid_d, TipoEvento.AI_RESPONSE_DISCARDED)
          == "STATE_VERSION_DIVERGENTE",
          "D4. 1 AI_RESPONSE_DISCARDED com error_code=STATE_VERSION_DIVERGENTE")
    snap_d = snapshot_conversa(cid_d)
    check(snap_d["state_version"] == 1 and snap_d["atendente_id"] == ID_CARLA,
          f"D5. o valor do segundo writer prevalece — a IA nunca sobrescreve ({snap_d})")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO E — valor monetario no output -> bloqueado pelo output_guard
    # ═══════════════════════════════════════════════════════════════════════
    print("\nE. Valor monetario na resposta -> output_guard bloqueia")
    lead_e = criar_lead("Lead E")
    cid_e = criar_conversa(lead_e)
    snap_e_antes = snapshot_conversa(cid_e)
    configurar_ia(comunicador_resposta="O pacote custa 5000 reais.")
    r = webhook(cid_e)
    corpo = r.json()
    check(r.status_code == 200, f"E1. 200 (o bloqueio e desfecho de negocio) (veio {r.status_code})")
    check(corpo.get("etapa_final") == "resposta_bloqueada_guard"
          and corpo.get("texto_resposta") is None,
          f"E2. desfecho resposta_bloqueada_guard, sem texto (veio {corpo})")
    check("5000" not in r.text, "E3. o valor monetario nao aparece em lugar nenhum do corpo HTTP")
    check(error_code_do_evento(cid_e, TipoEvento.AI_RESPONSE_DISCARDED)
          == "VALOR_MONETARIO_DETECTADO", "E4. error_code=VALOR_MONETARIO_DETECTADO")
    check(snapshot_conversa(cid_e) == snap_e_antes, "E5. NENHUMA coluna de conversations mudou")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO F — replay de event_id -> 409, zero efeito duplicado
    # ═══════════════════════════════════════════════════════════════════════
    print("\nF. Mesmo event_id duas vezes -> 409 sem efeito duplicado")
    lead_f = criar_lead("Lead F")
    cid_f = criar_conversa(lead_f)
    ev_f = str(uuid.uuid4())
    configurar_ia()
    r1 = webhook(cid_f, event_id=ev_f)
    check(r1.status_code == 200, f"F1. primeira chamada com event_id novo da 200 ({r1.status_code})")
    snap_f = snapshot_conversa(cid_f)
    chamadas_f = estado_ia()["interpretador_chamadas"]
    r2 = webhook(cid_f, event_id=ev_f)
    check(r2.status_code == 409 and r2.json().get("event_id") == ev_f,
          f"F2. replay do MESMO event_id da 409 (veio {r2.status_code}: {r2.text[:160]})")
    check(contar_eventos(cid_f, TipoEvento.MESSAGE_RECEIVED) == 1,
          "F3. a segunda chamada NAO produziu um segundo MESSAGE_RECEIVED")
    check(snapshot_conversa(cid_f) == snap_f,
          "F4. NENHUMA coluna de conversations mudou entre a 1a e a 2a chamada")
    check(estado_ia()["interpretador_chamadas"] == chamadas_f,
          "F5. a IA Interpretadora NAO foi chamada de novo — o replay e barrado antes da fronteira de IA")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO G1 — dry_run -> zero efeito colateral, agora verificavel NO CRM
    # ═══════════════════════════════════════════════════════════════════════
    # (O G de tests/test_v2_e2e_bia.py. Ver a ambiguidade 2 do docstring.)
    # No E2E anterior a prova de "o CRM nunca foi chamado" era um fake que
    # levantava AssertionError. Aqui a prova e melhor e nao depende de nenhuma
    # substituicao: o CRM e um servidor REAL com banco REAL, entao "nao foi
    # chamado" e observavel como ZERO linha no livro-razao dele.
    print("\nG1. V2_MODE=dry_run -> zero efeito, provado no banco dos DOIS lados")
    configurar_conversas(v2_mode="dry_run")
    lead_g1 = criar_lead("Lead G1")
    cid_g1 = criar_conversa(lead_g1)
    snap_g1_antes = snapshot_conversa(cid_g1)
    ops_antes = sql_valor("SELECT count(*) FROM handoff_operations")
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    r = webhook(cid_g1, mensagem=MSG_COMPLETA)
    corpo = r.json()
    check(r.status_code == 200 and corpo.get("etapa_final") == "handoff_simulado_dry_run"
          and corpo.get("atendente_id") == ID_ANA,
          f"G1a. desfecho handoff_simulado_dry_run com o atendente elegivel (veio {corpo})")
    check(snapshot_conversa(cid_g1) == snap_g1_antes,
          "G1b. ZERO mudanca em conversations para esta conversa")
    check(sql_valor("SELECT count(*) FROM handoff_operations") == ops_antes,
          "G1c. ZERO linha nova em handoff_operations")
    check(dono_no_crm(lead_g1) is None and linhas_livro_razao(lead_g1) == 0,
          "G1d. ZERO efeito no CRM REAL — nenhuma requisicao chegou ao livro-razao dele")
    check(estado_ia()["comunicador_chamadas"] == 0,
          "G1e. comunicador NUNCA chamado — dry_run tambem para antes do gate/comunicador")
    configurar_conversas(v2_mode="live")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO G2 — retomada concorrente + mensagem nova (R-B), ESPERADO VERDE
    # ═══════════════════════════════════════════════════════════════════════
    # O G do desenho (v3, secao 7): duas operacoes V2 diferentes, `event_id`
    # distintos e `target_user_id` DIFERENTES, alcancando o CRM ao mesmo tempo
    # pelo mesmo lead. Na v3 o criterio era ESPERADO VERMELHO — nada impedia as
    # duas de aplicarem. Com R-B implementado o criterio inverte (secao 8.5):
    # exatamente uma aplica, a outra recebe 409.
    #
    # Montagem, toda real: (1) o CRM recusa UMA requisicao com 503 ANTES da rota
    # (o CRM comprovadamente nao aplica), o que deixa a operacao da conversa 1
    # em FAILED/NAO_APLICADO_RETRIAVEL — fatia legitima de R16; (2) a
    # elegibilidade muda para o Bruno, entao uma mensagem nova na conversa 2 (do
    # MESMO lead) escolhe outro alvo; (3) as duas correm: o worker R16 reapresenta
    # o event_id antigo (Ana) enquanto o webhook novo pede o Bruno.
    print("\nG2. Retomada concorrente + mensagem nova, MESMO lead, alvos diferentes")
    lead_g2 = criar_lead("Lead G2")
    cid_g2a = criar_conversa(lead_g2)
    cid_g2b = criar_conversa(lead_g2)
    ev_g2a = str(uuid.uuid4())
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    crm_controle("status", codigo=503, requisicoes=1)
    webhook(cid_g2a, mensagem=MSG_COMPLETA, event_id=ev_g2a)
    op_g2a = operacao(ev_g2a)
    check(op_g2a is not None and op_g2a["status"] == "FAILED"
          and op_g2a["error_class"] in worker_fila.CLASSES_RETOMAVEIS_R16,
          f"G2a. a 1a operacao ficou FAILED com classe retomavel por R16 ({op_g2a})")
    check(dono_no_crm(lead_g2) is None and linhas_livro_razao(lead_g2) == 0,
          "G2b. a recusa aconteceu ANTES da rota — o CRM nao aplicou nada")

    configurar_conversas(atendentes_elegiveis=ID_BRUNO)
    _saida_g2 = {}

    def _retomar_g2():
        db = SessionLocal()
        try:
            _saida_g2["ciclo"] = worker_retomada.processar_ciclo(db)
        except Exception as exc:  # pragma: no cover - vira falha de check abaixo
            _saida_g2["erro"] = repr(exc)
        finally:
            db.close()

    def _mensagem_nova_g2():
        try:
            _saida_g2["webhook"] = webhook(cid_g2b, mensagem=MSG_COMPLETA).json()
        except Exception as exc:  # pragma: no cover
            _saida_g2["erro_webhook"] = repr(exc)

    _threads = [threading.Thread(target=_retomar_g2), threading.Thread(target=_mensagem_nova_g2)]
    for t in _threads:
        t.start()
    for t in _threads:
        t.join(timeout=120)

    check("erro" not in _saida_g2 and "erro_webhook" not in _saida_g2,
          f"G2c. as duas operacoes concorrentes rodaram sem excecao ({_saida_g2.get('erro')} "
          f"{_saida_g2.get('erro_webhook')})")
    _dono_g2 = dono_no_crm(lead_g2)
    check(linhas_livro_razao(lead_g2) == 1,
          f"G2d. EXATAMENTE UMA operacao aplicou no CRM — o livro-razao tem 1 linha "
          f"(veio {linhas_livro_razao(lead_g2)})")
    check(_dono_g2 in (ID_ANA, ID_BRUNO),
          f"G2e. o dono final e um dos dois alvos disputados, nao um terceiro valor ({_dono_g2})")
    _ops_g2 = sql(
        "SELECT o.status, o.error_code, o.target_user_id, o.conversation_id "
        "FROM handoff_operations o JOIN conversations c ON c.id = o.conversation_id "
        "WHERE c.lead_id = :l", l=lead_g2,
    )
    _vencedoras = [o for o in _ops_g2 if o[0] == "COMPLETED"]
    _perdedoras = [o for o in _ops_g2 if o[0] == "FAILED"]
    check(len(_vencedoras) == 1 and _vencedoras[0][2] == _dono_g2,
          f"G2f. exatamente UMA operacao do Conversas ficou COMPLETED, e o alvo dela e o "
          f"dono do CRM ({_ops_g2})")
    check(len(_perdedoras) == 1 and _perdedoras[0][1] == "CRM_LEAD_JA_ATRIBUIDO",
          f"G2g. a perdedora ficou FAILED/CRM_LEAD_JA_ATRIBUIDO — 409 real do CAS ({_ops_g2})")
    _cid_vencedora = _vencedoras[0][3]
    _cid_perdedora = _perdedoras[0][3]
    check(snapshot_conversa(_cid_perdedora)["state"] != "WAITING_HUMAN"
          and snapshot_conversa(_cid_perdedora)["atendente_id"] is None,
          "G2h. a conversa perdedora NUNCA aplicou estado local")
    criterio_global("G2i", _cid_vencedora, lead_g2)
    configurar_conversas(atendentes_elegiveis=ID_ANA)

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO H — falha local PERMANENTE depois de o CRM ja ter aplicado
    # ═══════════════════════════════════════════════════════════════════════
    # O H do desenho (v3, secao 7). O CRM aplica; a aplicacao local e recusada
    # pela maquina de estados porque um humano REAL respondeu no meio do
    # round-trip (o cenario F1 documentado em `_aplicar_estado_local`:
    # `primeira_resposta_humana_at` preenchido e `queued_at` zerado derrubam as
    # invariantes 5 e 6 de WAITING_HUMAN). A janela e produzida pelo atraso
    # DETERMINISTICO da resposta do CRM — o commit dele ja aconteceu.
    #
    # A linha resultante e `ESTADO_LOCAL_RECUSADO`, fatia EXCLUSIVA de R15
    # (secao 4.2) — e o cenario tambem prova essa particao comportamentalmente.
    print("\nH. CRM aplica, aplicacao local recusada em definitivo -> R15 (Direcao B) converge")
    lead_h = criar_lead("Lead H")
    cid_h = criar_conversa(lead_h)
    ev_h = str(uuid.uuid4())
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    crm_controle("atraso", ms=2500, requisicoes=1)
    _resp_h = {}

    def _webhook_h():
        try:
            _r_h = webhook(cid_h, mensagem=MSG_COMPLETA, event_id=ev_h)
            _resp_h["status"] = _r_h.status_code
            _resp_h["corpo"] = _r_h.json()
        except Exception as exc:  # pragma: no cover
            _resp_h["erro"] = repr(exc)

    _t_h = threading.Thread(target=_webhook_h)
    _t_h.start()
    esperar_atraso_iniciado()  # poll ate o CRM comecar a represar a resposta
    # Humano REAL assume e responde: os mesmos campos que
    # `marcar_atendimento_humano` grava (`app/services/outbound.py`).
    executar(
        "UPDATE conversations SET atendente_id = :u, responsavel_id = :u, "
        "primeira_resposta_humana_at = now(), queued_at = NULL, is_bot_active = false "
        "WHERE id = :c", u=ID_HELENA, c=cid_h,
    )
    _t_h.join(timeout=120)

    check("erro" not in _resp_h, f"H1. o webhook retornou sem excecao ({_resp_h.get('erro')})")
    check(_resp_h.get("status") == 200,
          f"H1b. o webhook respondeu 200 (veio {_resp_h.get('status')}: "
          f"{str(_resp_h.get('corpo'))[:160]})")
    check(dono_no_crm(lead_h) == ID_ANA and linhas_livro_razao(lead_h) == 1,
          f"H2. o CRM APLICOU (responsavel={dono_no_crm(lead_h)}, 1 linha no livro-razao) — "
          f"a falha veio depois, do lado local")
    op_h = operacao(ev_h)
    check(op_h is not None and op_h["status"] == "FAILED"
          and op_h["error_code"] == "ESTADO_LOCAL_RECUSADO" and op_h["error_class"] is None,
          f"H3. operacao FAILED/ESTADO_LOCAL_RECUSADO com error_class NULL ({op_h})")
    check(snapshot_conversa(cid_h)["state"] != "WAITING_HUMAN",
          "H4. a conversa NAO foi para WAITING_HUMAN — a transicao foi recusada antes de mutar")
    _db = SessionLocal()
    try:
        _ciclo_r16 = worker_retomada.processar_ciclo(_db)
    finally:
        _db.close()
    check(op_h["id"] not in _ciclo_r16.reclamadas,
          f"H5. PARTICAO: R16 NAO reclama uma linha ESTADO_LOCAL_RECUSADO "
          f"(reclamadas={_ciclo_r16.reclamadas})")
    _db = SessionLocal()
    try:
        _ciclo_r15 = worker_reconciliacao.processar_ciclo(_db)
    finally:
        _db.close()
    check(op_h["id"] in _ciclo_r15.reclamadas,
          f"H6. PARTICAO: R15 reclama a linha (reclamadas={_ciclo_r15.reclamadas})")
    op_h2 = operacao(ev_h)
    check(dono_no_crm(lead_h) == ID_HELENA,
          f"H7. R15 Direcao B corrigiu o CRM para o humano local pelo endpoint com CAS "
          f"(dono={dono_no_crm(lead_h)}, esperado {ID_HELENA})")
    check(op_h2["reconciled_at"] is not None,
          f"H8. a linha foi marcada reconciled_at — convergencia confirmada por comparacao ({op_h2})")
    criterio_global("H9", cid_h, lead_h)

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO I — 2 requisicoes HTTP REAIS concorrentes, mesmo lead
    # ═══════════════════════════════════════════════════════════════════════
    # Secao 8.5, I. Codigo do CRM sem NENHUMA substituicao — o que roda aqui e
    # `aplicar_handoff` como ele esta no repositorio. A sincronizacao e do lado
    # CLIENTE (uma `Barrier` neste processo, so para os dois `post` sairem
    # juntos); o servidor nao sabe que existe teste.
    print("\nI. 2 requisicoes HTTP REAIS concorrentes, mesmo lead, alvos diferentes")
    RODADAS_I = 10
    _aplicacoes_i, _tokens_i, _donos_i = [], [], []
    for _r in range(RODADAS_I):
        _lead = criar_lead(f"Lead I {_r}")
        _barreira_i = threading.Barrier(2)
        _resultados = [None, None]
        _alvos = (ID_ANA, ID_BRUNO)

        def _disputar_i(indice, lead_id=_lead, barreira=_barreira_i):
            barreira.wait(timeout=30)
            _resultados[indice] = crm_handoff(
                event_id=str(uuid.uuid4()), lead_id=lead_id,
                target_user_id=_alvos[indice], expected=None,
            )

        _ts = [threading.Thread(target=_disputar_i, args=(i,)) for i in (0, 1)]
        for t in _ts:
            t.start()
        for t in _ts:
            t.join(timeout=60)
        _aplicou = [x for x in _resultados
                    if x is not None and x.status_code == 200 and x.json().get("replayed") is False]
        _recusou = [x for x in _resultados if x is not None and x.status_code == 409]
        _aplicacoes_i.append(len(_aplicou))
        _donos_i.append((
            dono_no_crm(_lead),
            _aplicou[0].json()["target_user_id"] if _aplicou else None,
            [x.json()["detail"].get("current_responsavel_id") for x in _recusou
             if isinstance(x.json().get("detail"), dict)],
            linhas_livro_razao(_lead),
        ))
        for x in _recusou:
            _d = x.json().get("detail")
            _tokens_i.append(_d.get("error") if isinstance(_d, dict) else _d)

    check(all(v == 1 for v in _aplicacoes_i),
          f"I1. EXATAMENTE UMA aplica em cada uma das {RODADAS_I} rodadas "
          f"(distribuicao: {sorted(set(_aplicacoes_i))})")
    check(bool(_tokens_i) and all(t == "LEAD_ALREADY_ASSIGNED" for t in _tokens_i),
          f"I2. toda perdedora recebe 409 LEAD_ALREADY_ASSIGNED (tokens: {sorted(set(_tokens_i))})")
    check(all(d[0] == d[1] and d[2] == [d[0]] and d[3] == 1 for d in _donos_i),
          f"I3. o 409 traz `current_responsavel_id` igual ao dono real, e o livro-razao "
          f"tem 1 linha por lead ({_donos_i[:3]}...)")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO J — 3 operacoes concorrentes, mesmo lead, um vencedor
    # ═══════════════════════════════════════════════════════════════════════
    print("\nJ. 3 requisicoes HTTP REAIS concorrentes, mesmo lead")
    lead_j = criar_lead("Lead J")
    _barreira_j = threading.Barrier(3)
    _resultados_j = [None, None, None]
    _alvos_j = (ID_ANA, ID_BRUNO, ID_CARLA)

    def _disputar_j(indice):
        _barreira_j.wait(timeout=30)
        _resultados_j[indice] = crm_handoff(
            event_id=str(uuid.uuid4()), lead_id=lead_j,
            target_user_id=_alvos_j[indice], expected=None,
        )

    _ts = [threading.Thread(target=_disputar_j, args=(i,)) for i in range(3)]
    for t in _ts:
        t.start()
    for t in _ts:
        t.join(timeout=60)
    _aplicou_j = [x for x in _resultados_j
                  if x is not None and x.status_code == 200 and x.json().get("replayed") is False]
    _recusou_j = [x for x in _resultados_j if x is not None and x.status_code == 409]
    check(len(_aplicou_j) == 1,
          f"J1. EXATAMENTE UM dos 3 aplica (status: {[x.status_code if x else None for x in _resultados_j]})")
    check(len(_recusou_j) == 2, f"J2. os outros 2 recebem 409 ({len(_recusou_j)})")
    check(all(isinstance(x.json().get("detail"), dict)
              and x.json()["detail"].get("error") == "LEAD_ALREADY_ASSIGNED"
              for x in _recusou_j),
          "J3. os dois 409 trazem o token estruturado LEAD_ALREADY_ASSIGNED")
    check(linhas_livro_razao(lead_j) == 1 and dono_no_crm(lead_j)
          == (_aplicou_j[0].json()["target_user_id"] if _aplicou_j else None),
          f"J4. livro-razao com 1 linha e dono = alvo do vencedor (dono={dono_no_crm(lead_j)})")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO K — retry depois de timeout AMBIGUO em que o CRM ja comitou
    # ═══════════════════════════════════════════════════════════════════════
    # O timeout e REAL: a requisicao sai pela rede, o CRM aplica e COMITA, e a
    # resposta fica represada por mais tempo do que o `read` do cliente aceita.
    # `_chamar_crm` classifica `ReadTimeout` como AMBIGUO_RETRIAVEL (fatia de
    # R16), a conversa NAO vai para WAITING_HUMAN, e a divergencia fica de pe
    # ate a retomada.
    print("\nK. Timeout ambiguo (CRM ja comitou) -> R16 retoma e recebe replayed=true")
    lead_k = criar_lead("Lead K")
    cid_k = criar_conversa(lead_k)
    ev_k = str(uuid.uuid4())
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    configurar_conversas(timeout_crm_read=1.0)
    crm_controle("atraso", ms=3000, requisicoes=1)
    _r_k = webhook(cid_k, mensagem=MSG_COMPLETA, event_id=ev_k)
    configurar_conversas(timeout_crm_read=10.0)
    check(_r_k.status_code == 200,
          f"K0. o webhook responde 200 mesmo com o timeout ambiguo do CRM represado "
          f"(veio {_r_k.status_code}: {_r_k.text[:160]})")
    op_k = operacao(ev_k)
    check(op_k is not None and op_k["status"] == "FAILED"
          and op_k["error_code"] == "CRM_INDISPONIVEL"
          and op_k["error_class"] == "AMBIGUO_RETRIAVEL",
          f"K1. operacao FAILED/CRM_INDISPONIVEL/AMBIGUO_RETRIAVEL — timeout de leitura real ({op_k})")
    check(snapshot_conversa(cid_k)["state"] != "WAITING_HUMAN"
          and snapshot_conversa(cid_k)["atendente_id"] is None,
          "K2. o estado local NAO foi aplicado — o Conversas nao sabe se o CRM aplicou")
    check(dono_no_crm(lead_k) == ID_ANA and linhas_livro_razao(lead_k) == 1,
          f"K3. mas o CRM APLICOU e COMITOU antes do timeout (dono={dono_no_crm(lead_k)})")
    _replay = crm_handoff(event_id=ev_k, lead_id=lead_k, target_user_id=ID_ANA, expected=None)
    check(_replay.status_code == 200 and _replay.json().get("replayed") is True
          and _replay.json().get("target_user_id") == ID_ANA
          and _replay.json().get("current_responsavel_id") == ID_ANA,
          f"K4. o retry do MESMO event_id recebe 200 replayed=true com o alvo ja aplicado "
          f"({_replay.status_code}: {_replay.text[:160]})")
    _db = SessionLocal()
    try:
        _ciclo_k = worker_retomada.processar_ciclo(_db)
    finally:
        _db.close()
    op_k2 = operacao(ev_k)
    check(op_k["id"] in _ciclo_k.reclamadas and op_k2["status"] == "COMPLETED",
          f"K5. R16 (worker real) reclamou a linha e a completou ({_ciclo_k}, {op_k2})")
    check(linhas_livro_razao(lead_k) == 1,
          f"K6. a retomada NAO criou uma segunda aplicacao no CRM ({linhas_livro_razao(lead_k)} linha)")
    criterio_global("K7", cid_k, lead_k)

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO L — nova atribuicao depois de release LEGITIMO, pela BASELINE
    # ═══════════════════════════════════════════════════════════════════════
    # Caso 6 do desenho. O release e feito pela rota V1 REAL
    # (`PUT /api/leads/{id}/responsavel` sem `responsavel_id`), que zera o dono.
    # A prova de que a aceitacao vem da BASELINE e nao do TEMPO: com o lead ja
    # liberado, uma requisicao com baseline OBSOLETA (o dono antigo) e recusada
    # ANTES de a correta ser aceita — a ordem cronologica favorece a errada, e
    # ela perde mesmo assim.
    print("\nL. Release legitimo -> nova atribuicao aceita pela baseline, nao pelo tempo")
    lead_l = criar_lead("Lead L")
    _r0 = crm_handoff(event_id=str(uuid.uuid4()), lead_id=lead_l,
                      target_user_id=ID_ANA, expected=None)
    check(_r0.status_code == 200 and dono_no_crm(lead_l) == ID_ANA,
          f"L1. primeira atribuicao aplica (dono={dono_no_crm(lead_l)})")
    _rel = crm_release(lead_l)
    check(_rel.status_code == 200 and dono_no_crm(lead_l) is None,
          f"L2. release legitimo pela rota V1 REAL zerou leads.responsavel_id "
          f"({_rel.status_code}, dono={dono_no_crm(lead_l)})")
    _stale = crm_handoff(event_id=str(uuid.uuid4()), lead_id=lead_l,
                         target_user_id=ID_CARLA, expected=ID_ANA)
    check(_stale.status_code == 409
          and _stale.json()["detail"].get("error") == "LEAD_ALREADY_ASSIGNED"
          and _stale.json()["detail"].get("current_responsavel_id") is None,
          f"L3. baseline OBSOLETA e recusada com 409 mesmo chegando DEPOIS do release "
          f"({_stale.status_code}: {_stale.text[:160]})")
    _nova = crm_handoff(event_id=str(uuid.uuid4()), lead_id=lead_l,
                        target_user_id=ID_BRUNO, expected=None)
    check(_nova.status_code == 200 and _nova.json().get("replayed") is False
          and dono_no_crm(lead_l) == ID_BRUNO,
          f"L4. baseline CORRETA (None, o lead esta sem dono) aplica sem TTL nem espera "
          f"({_nova.status_code}, dono={dono_no_crm(lead_l)})")
    check(linhas_livro_razao(lead_l) == 2,
          f"L5. livro-razao tem 2 linhas (as duas aceitas); a recusada nao gravou nada "
          f"({linhas_livro_razao(lead_l)})")

    # ═══════════════════════════════════════════════════════════════════════
    # CENARIO M — R16 reenvia o event_id ORIGINAL depois de reatribuicao legitima
    # ═══════════════════════════════════════════════════════════════════════
    # Secao 8.5, M — o cenario que so existe porque o Caso 2b (secao 2.1.1)
    # fechou uma lacuna que tornava R16 perigoso. A falha local e de motivo
    # RETRIAVEL (timeout ambiguo), NUNCA `ESTADO_LOCAL_RECUSADO`: essa classe e
    # exclusiva de R15 pela particao do §4.2, e usa-la aqui contradiria a
    # premissa do cenario (R16 precisa ser o dono legitimo desta linha).
    print("\nM. R16 reenvia o event_id original depois de reatribuicao -> 409, nunca replay cego")
    lead_m = criar_lead("Lead M")
    cid_m = criar_conversa(lead_m)
    ev_m = str(uuid.uuid4())
    configurar_ia(extracted=TRIAGEM_COMPLETA)
    configurar_conversas(timeout_crm_read=1.0)
    crm_controle("atraso", ms=3000, requisicoes=1)
    _r_m = webhook(cid_m, mensagem=MSG_COMPLETA, event_id=ev_m)
    configurar_conversas(timeout_crm_read=10.0)
    check(_r_m.status_code == 200,
          f"M0. o webhook responde 200 mesmo com o timeout ambiguo do CRM represado "
          f"(veio {_r_m.status_code}: {_r_m.text[:160]})")
    op_m = operacao(ev_m)
    check(op_m is not None and op_m["status"] == "FAILED"
          and op_m["error_class"] in worker_fila.CLASSES_RETOMAVEIS_R16
          and op_m["error_code"] != "ESTADO_LOCAL_RECUSADO",
          f"M1. a operacao E falhou por motivo RETRIAVEL (fatia de R16), nao "
          f"ESTADO_LOCAL_RECUSADO ({op_m})")
    check(dono_no_crm(lead_m) == ID_ANA,
          f"M2. o CRM aplicou o alvo T={ID_ANA} antes do timeout (dono={dono_no_crm(lead_m)})")
    snap_m_antes = snapshot_conversa(cid_m)

    # Reatribuicao LEGITIMA (Caso 6): release pela rota V1 real, depois nova
    # operacao V2 com a baseline correta.
    crm_release(lead_m)
    _reat = crm_handoff(event_id=str(uuid.uuid4()), lead_id=lead_m,
                        target_user_id=ID_BRUNO, expected=None)
    check(_reat.status_code == 200 and dono_no_crm(lead_m) == ID_BRUNO,
          f"M3. o lead foi legitimamente reatribuido a U={ID_BRUNO} (dono={dono_no_crm(lead_m)})")

    _replay_m = crm_handoff(event_id=ev_m, lead_id=lead_m, target_user_id=ID_ANA, expected=None)
    _detalhe_m = _replay_m.json().get("detail")
    check(_replay_m.status_code == 409 and isinstance(_detalhe_m, dict)
          and _detalhe_m.get("error") == "LEAD_ALREADY_ASSIGNED"
          and _detalhe_m.get("current_responsavel_id") == ID_BRUNO,
          f"M4. reenviar o event_id ORIGINAL devolve 409 LEAD_ALREADY_ASSIGNED com "
          f"current_responsavel_id=U, NAO 200 replayed=true com T "
          f"({_replay_m.status_code}: {_replay_m.text[:200]})")

    _db = SessionLocal()
    try:
        _ciclo_m = worker_retomada.processar_ciclo(_db)
    finally:
        _db.close()
    op_m2 = operacao(ev_m)
    check(op_m["id"] in _ciclo_m.reclamadas,
          f"M5. R16 (worker real) reclamou a linha — ele E o dono legitimo dela "
          f"({_ciclo_m.reclamadas})")
    check(op_m2["status"] == "FAILED" and op_m2["error_code"] == "CRM_LEAD_JA_ATRIBUIDO"
          and op_m2["error_class"] == "NAO_APLICADO_TERMINAL",
          f"M6. a retomada gravou CRM_LEAD_JA_ATRIBUIDO/NAO_APLICADO_TERMINAL ({op_m2})")
    _snap_m_depois = snapshot_conversa(cid_m)
    check(_snap_m_depois["state"] == snap_m_antes["state"]
          and _snap_m_depois["state_version"] == snap_m_antes["state_version"]
          and _snap_m_depois["atendente_id"] is None
          and _snap_m_depois["responsavel_id"] is None,
          f"M7. `_aplicar_estado_local` NUNCA rodou para este replay — a conversa nao "
          f"recebeu o alvo ja superado ({_snap_m_depois})")
    check(contar_eventos(cid_m, TipoEvento.HUMAN_SELECTED) == 0
          and contar_eventos(cid_m, TipoEvento.HANDOFF_COMPLETED) == 0,
          "M8. ZERO HUMAN_SELECTED/HANDOFF_COMPLETED — nada foi aplicado localmente")
    check(dono_no_crm(lead_m) == ID_BRUNO,
          f"M9. o CRM continua com U — a retomada nao reverteu para T (dono={dono_no_crm(lead_m)})")
    # `next_attempt_at` volta a NULL ANTES do segundo ciclo, de proposito: a
    # reclamacao de M5 adiou a linha em 60s, e sem zerar isto o ciclo abaixo
    # deixaria de reclama-la pela JANELA DE BACKOFF, nao pela particao — o check
    # passaria pelo motivo errado, provando nada sobre §4.2.
    executar("UPDATE handoff_operations SET next_attempt_at = NULL WHERE id = :i",
             i=op_m["id"])
    _db = SessionLocal()
    try:
        _ciclo_m2 = worker_retomada.processar_ciclo(_db)
    finally:
        _db.close()
    check(op_m["id"] not in _ciclo_m2.reclamadas,
          f"M10. com a janela de backoff zerada, R16 AINDA NAO reclama a linha — ela "
          f"mudou de dono para R15 pelo error_code (particao do §4.2) ({_ciclo_m2.reclamadas})")
    _db = SessionLocal()
    try:
        _ciclo_m3 = worker_reconciliacao.processar_ciclo(_db)
    finally:
        _db.close()
    check(op_m["id"] in _ciclo_m3.reclamadas,
          f"M11. e R15 reclama — as duas fatias sao disjuntas e a linha esta na certa "
          f"({_ciclo_m3.reclamadas})")

    # ═══════════════════════════════════════════════════════════════════════
    # TESTE NEGATIVO OBRIGATORIO (secao 2.3 / 8.5) — SOBRE HTTP REAL
    # ═══════════════════════════════════════════════════════════════════════
    # `tests/test_v2_rb_cas_postgres.py` ja prova, in-process, que a forma sem
    # `populate_existing()` falha. O que SO este nivel acrescenta e que a
    # deteccao sobrevive ao round-trip de rede contra um `uvicorn` real — ou
    # seja, que aquele resultado nao era artefato da serializacao do TestClient.
    # As duas variantes diferem em UMA unica coisa: se a leitura sob lock
    # re-hidrata o objeto do identity map. Pre-carga, barreira, lead, alvos e
    # numero de rodadas sao identicos.
    print("\nNEG. Experimento controlado sobre HTTP real: com e sem populate_existing()")
    RODADAS_NEG = 8

    def _rodada_dupla(lead_id):
        _res = [None, None]
        _alvos = (ID_ANA, ID_BRUNO)

        def _ir(indice):
            _res[indice] = crm_handoff(
                event_id=str(uuid.uuid4()), lead_id=lead_id,
                target_user_id=_alvos[indice], expected=None, timeout=90.0,
            )

        _ts = [threading.Thread(target=_ir, args=(i,)) for i in (0, 1)]
        for t in _ts:
            t.start()
        for t in _ts:
            t.join(timeout=120)
        return sum(1 for x in _res
                   if x is not None and x.status_code == 200
                   and x.json().get("replayed") is False)

    crm_controle("variante-lock", modo="barreira_correta")
    _ok = [_rodada_dupla(criar_lead(f"Lead NEG OK {i}")) for i in range(RODADAS_NEG)]
    _ident_ok = crm_identidades()

    crm_controle("variante-lock", modo="barreira_quebrada")
    _quebrado = [_rodada_dupla(criar_lead(f"Lead NEG QUEBRADO {i}")) for i in range(RODADAS_NEG)]
    _ident_quebrado = crm_identidades()
    crm_controle("variante-lock", modo="producao")

    _perdidos = sum(1 for v in _quebrado if v > 1)
    print(f"     rodadas com update perdido na forma quebrada: {_perdidos}/{RODADAS_NEG} "
          f"(distribuicao: {sorted(set(_quebrado))})")
    check(bool(_ident_ok) and all(_ident_ok),
          f"NEG1. na forma CORRETA a leitura sob lock reencontrou o MESMO objeto Python da "
          f"pre-carga — o identity map estava sujo e `populate_existing()` re-hidratou "
          f"({sorted(set(_ident_ok))})")
    check(all(v == 1 for v in _ok),
          f"NEG2. forma CORRETA: exatamente uma aplica em todas as {RODADAS_NEG} rodadas, "
          f"mesmo com o identity map sujo ({sorted(set(_ok))})")
    check(bool(_ident_quebrado) and all(_ident_quebrado),
          f"NEG3. na forma QUEBRADA a leitura sob lock devolveu o objeto OBSOLETO — o "
          f"mecanismo do bug e o identity map, nao ausencia de lock ({sorted(set(_ident_quebrado))})")
    check(_perdidos > 0,
          f"NEG4. a forma SEM populate_existing FALHA sobre HTTP real — as duas operacoes "
          f"aplicam ({_perdidos}/{RODADAS_NEG}). Se este check passar a falhar, o teste "
          f"negativo perdeu poder de deteccao e I/J/NEG2 deixaram de significar alguma "
          f"coisa: investigue a montagem ANTES de confiar no positivo")
    check(_perdidos >= RODADAS_NEG // 2,
          f"NEG5. a falha da forma errada e sistematica, nao acaso raro "
          f"({_perdidos}/{RODADAS_NEG}; esperado >= {RODADAS_NEG // 2})")
    check(all(v == 1 for v in _ok) and _perdidos > 0,
          "NEG6. `populate_existing()` e a UNICA variavel que muda o desfecho — mesmo "
          "cenario, mesma pre-carga, mesma barreira, mesmo transporte, resultados opostos")

finally:
    derrubar()

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} CHECK(S) FALHARAM:")
    for f in failures:
        print(f"  - {f}")
    despejar_logs()
    sys.exit(1)
print("E2E DISTRIBUIDO: cenarios A-M verdes + teste negativo com poder de deteccao.")
sys.exit(0)
