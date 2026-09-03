"""
BIA-V2 Fase 0 / Task 0.3 - guarda de regressao da V1.

Familia A (IMPLEMENTADA): hash SHA-256 dos 8 arquivos V1 explicitamente
enumerados pela Task 0.3 do plano como baseline imutavel
(docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Task 0.3"). Nenhum
codigo de app e importado aqui - so leitura de arquivo e comparacao de hash.

Familia B (ATIVADA na Fase 4): ver o bloco proprio mais abaixo. Implementa
as 5 provas de independencia semantica (B.1 a B.5) que substituem os dois
tripwires originais - nunca os silencia.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_v1_intacta.py
"""
import hashlib
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONVERSAS_DIR = ROOT / "conversas"

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


def _sha256_normalizado(caminho: pathlib.Path) -> str:
    """SHA-256 dos bytes do arquivo (lido em modo binario) apos normalizar
    fim-de-linha CRLF -> LF. Ver justificativa no comentario da Familia A."""
    dados = caminho.read_bytes()
    return hashlib.sha256(dados.replace(b"\r\n", b"\n")).hexdigest()


# ============================================================================
# FAMILIA A - imutabilidade de arquivo (IMPLEMENTADA)
# ----------------------------------------------------------------------------
# Estes sao os 8 arquivos explicitamente protegidos por hash na Task 0.3.
# Nenhum pode mudar enquanto a V2 for construida em paralelo.
#
# ESCOPO DESTA BASELINE - leia antes de concluir cobertura:
# A Global Constraint e mais ampla que esta baseline de 8 arquivos. Nesta task,
# mudancas V1 fora desta lista continuam proibidas, mas NAO sao cobertas por
# esta guarda de hash; sua deteccao depende dos demais gates de diff/review.
# Ampliar a guarda de hash exige auditoria propria do conjunto completo, nao
# inclusao ad hoc de dependencias isoladas.
#
# POR QUE HASH NORMALIZADO (CRLF -> LF) EM VEZ DO BYTE CRU:
# medido em HEAD 22f9fb24f117197bb59c7248ce88beb6acebcff7: os 8 blobs
# gravados no git sao puramente LF. Este repo tem `core.autocrlf=true` e
# NENHUM `.gitattributes`; os dois jobs do CI (.github/workflows/test.yml)
# rodam em `ubuntu-latest`, onde o checkout entrega o byte do blob tal como
# esta - LF. Neste working tree Windows, porem, 5 dos 8 arquivos
# (outbound.py, webhook.py, conversations.py, leads.py, lead_creation.py)
# ja estao materializados em CRLF no disco; os outros 3 ja estao em LF -
# sem que `git status` acuse nada, porque o git normaliza a comparacao na
# hora de decidir se ha mudanca. Um baseline de byte CRU travaria esses 5
# arquivos em TODA rodada de CI, por um motivo que nao e ninguem ter tocado
# a V1 - e nem seria estavel localmente, ja que o fim-de-linha em disco pode
# ser reescrito por qualquer ferramenta sem isso ser uma mudanca de verdade.
# O preco pago: esta guarda NAO detecta uma troca PURA de fim-de-linha sem
# nenhuma outra alteracao. E um preco aceitavel - o proprio git ja nao trata
# isso como mudanca, nao altera semantica Python nenhuma, e a alternativa e
# uma guarda que grita falso-positivo em toda rodada de CI ate alguem
# desativa-la.
#
# ATUALIZACAO DO BASELINE E MANUAL E DELIBERADA. Uma mudanca aprovada na V1
# exige recalcular o digest (mesma formula acima) e colar o valor aqui a
# mao, por decisao humana. NUNCA adicione um jeito de regenerar isto sozinho
# - nada de flag --update, env var, ou "se nao bate, grava o atual": isso
# transformaria a guarda em um no-op disfarcado.
_PROTEGIDOS = (
    (CONVERSAS_DIR / "app/services/atendimento.py",
     "e7031041d8d302184e1a161b68bef861e34f3e28f1ddd85e37802929aaf95360"),
    (CONVERSAS_DIR / "app/services/outbound.py",
     "02ad2c4e2f4c07e5a4933adb333a02ddc516d197e01106b1d4710fe0caf6dd77"),
    (CONVERSAS_DIR / "app/routers/webhook.py",
     "5b5c23184a28ab293247dff4371675fc0e73ea31603e22db39755b92c7d95f90"),
    (CONVERSAS_DIR / "app/routers/conversations.py",
     "0d7463e2128862cc5260481b9826e29c56b8e8294895cca5ef5b8555636d8e6f"),
    (CONVERSAS_DIR / "app/services/crm.py",
     "2d96421cd1421bb73f2dbd6ced73e188f4b2df9cd8a9f496791f71db14316ac5"),
    (ROOT / "app/routers/leads.py",
     "2c64ba21f835dbf0ff69850de236c282b355afd90e3822cd927b3863a9a1bce4"),
    (ROOT / "app/services/lead_creation.py",
     "cad719206f354a51c3273c6995f0d2832b3d58e1629b6b19d8b4f56abc2a4b58"),
    (ROOT / "app/services/conversas_bridge.py",
     "37ead9905fd497d0f8650a75c57e1f546a70182b579df2c519c0f127b4725ed4"),
)

for caminho, esperado in _PROTEGIDOS:
    if not caminho.is_file():
        check(False, f"ARQUIVO AUSENTE (nao e divergencia de hash): {caminho}")
        continue
    atual = _sha256_normalizado(caminho)
    check(atual == esperado, f"{caminho}: esperado={esperado} atual={atual}")


# ============================================================================
# FAMILIA B - independencia semantica das colunas V2 (ATIVADA na Fase 4)
# ----------------------------------------------------------------------------
# Ate a Fase 3 este bloco era um TRIPWIRE: falhava de proposito no instante em
# que `state`/`state_version`/`closed_at` aparecessem, para que a ausencia das
# provas nunca passasse despercebida. A Fase 4 criou as colunas, o tripwire
# disparou, e as cinco provas abaixo o SUBSTITUEM - nunca o silenciam.
#
# O que se prova aqui NAO e invisibilidade: o model `Conversation` e UNICO e
# compartilhado, entao o ORM da V1 passa a CARREGAR as tres colunas em todo
# SELECT. A garantia correta e que nenhum fluxo da V1 LE, ESCREVE ou RAMIFICA
# sobre elas.
# ============================================================================

# --- B.1 - nenhum fluxo V1 referencia as colunas novas ----------------------
# Duas provas sobre os mesmos 8 arquivos, porque uma sozinha e fraca:
#   (a) AST - pega `conversation.state`, `getattr(x, "state")`, `c["state"]` e
#       atribuicao, que e o que "ler/escrever/ramificar" realmente significa;
#   (b) grep de palavra inteira fora de comentario, como a Task 0.3 especifica.
# `state` exige `\b` dos dois lados: `status` e `_STATUS_ABERTOS` nao contam.
import ast as _ast
import tokenize as _tokenize

_COLUNAS_V2 = ("state", "state_version", "closed_at")
_RE_PALAVRA = re.compile(r"\b(" + "|".join(_COLUNAS_V2) + r")\b")


def _sem_comentarios(caminho):
    """Fonte com os tokens COMMENT removidos - a Task 0.3 exige 'fora de comentario'."""
    with open(caminho, "rb") as fh:
        try:
            toks = list(_tokenize.tokenize(fh.readline))
        except (_tokenize.TokenError, SyntaxError, IndentationError, TabError):
            return caminho.read_text(encoding="utf-8")
    # t[:2] descarta a posicao original de cada token, entao untokenize() cai no
    # modo "compat": renumera linhas contando NEWLINE/NL, nao pela posicao real do
    # source. Inofensivo para os 8 arquivos protegidos (nenhum usa continuacao de
    # linha "\"); se algum passar a usar, so o NUMERO DE LINHA de uma falha futura
    # ficaria incorreto - o resultado do check em si continuaria correto.
    return _tokenize.untokenize(
        [t[:2] for t in toks if t.type not in (_tokenize.COMMENT, _tokenize.ENCODING)]
    )


def _referencias_ast(caminho):
    """Toda referencia sintatica as tres colunas: atributo, getattr, subscript, nome."""
    arvore = _ast.parse(caminho.read_text(encoding="utf-8"))
    achados = []
    for no in _ast.walk(arvore):
        if isinstance(no, _ast.Attribute) and no.attr in _COLUNAS_V2:
            achados.append(f"{caminho.name}:{no.lineno} .{no.attr}")
        elif isinstance(no, _ast.Name) and no.id in _COLUNAS_V2:
            achados.append(f"{caminho.name}:{no.lineno} nome {no.id}")
        elif isinstance(no, _ast.Constant) and no.value in _COLUNAS_V2:
            achados.append(f"{caminho.name}:{no.lineno} literal {no.value!r}")
    return achados


# --- B.1 (prova direta) - _sem_comentarios() remove SO o comentario --------
# Achado da revisao independente: nenhum dos 8 arquivos protegidos contem a
# palavra "state" (dentro ou fora de comentario) - o loop abaixo passaria hoje
# mesmo se _sem_comentarios() estivesse quebrado. Prova o helper isolado, com
# uma fonte sintetica, sem depender do conteudo real dos 8 arquivos.
_fonte_sintetica = (
    "x = 1  # state usado so em comentario\n"
    "state = 1\n"
)
_arq_sintetico = ROOT / "scratch" / "v2_v1_intacta_sem_comentarios.py"
_arq_sintetico.parent.mkdir(exist_ok=True)
_arq_sintetico.write_text(_fonte_sintetica, encoding="utf-8")
try:
    _linhas_sinteticas = _sem_comentarios(_arq_sintetico).splitlines()
finally:
    _arq_sintetico.unlink()

check(
    len(_linhas_sinteticas) == 2 and not _RE_PALAVRA.search(_linhas_sinteticas[0]),
    f"B.1 direto: 'state' SO em comentario e removido por _sem_comentarios() ({_linhas_sinteticas!r})",
)
check(
    len(_linhas_sinteticas) == 2 and bool(_RE_PALAVRA.search(_linhas_sinteticas[1])),
    f"B.1 direto: 'state' em codigo real continua presente - helper nao e tolerante demais ({_linhas_sinteticas!r})",
)


for _caminho, _ in _PROTEGIDOS:
    if not _caminho.is_file():
        continue
    _ast_hits = _referencias_ast(_caminho)
    check(not _ast_hits, f"B.1 AST: {_caminho.name} nao referencia as colunas V2 ({_ast_hits[:3]})")

    _grep_hits = []
    for _n, _linha in enumerate(_sem_comentarios(_caminho).splitlines(), 1):
        if _RE_PALAVRA.search(_linha):
            _grep_hits.append(f"{_caminho.name}:{_n}")
    check(not _grep_hits, f"B.1 grep: {_caminho.name} sem ocorrencia fora de comentario ({_grep_hits[:3]})")


# --- bootstrap do app, so a partir daqui (B.2 a B.5 precisam da V1 rodando) --
import os  # noqa: E402

_DB = ROOT / "scratch" / "v2_v1_intacta_famB.db"
_DB.parent.mkdir(exist_ok=True)
if _DB.exists():
    _DB.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text as _sa_text  # noqa: E402
import sqlalchemy.exc  # noqa: E402

import app.main as _main  # noqa: E402
from app.auth import User as _User, get_current_user  # noqa: E402
from app.database import Base as _Base, SessionLocal as _Session, engine as _engine  # noqa: E402
from app.models.conversation import Conversation as _Conversation  # noqa: E402

_Base.metadata.create_all(bind=_engine)

_db = _Session()
_u1 = _User(nome="Um", email="b1@x.com", hashed_password="x", is_active=True)
_u2 = _User(nome="Dois", email="b2@x.com", hashed_password="x", is_active=True)
_db.add_all([_u1, _u2])
_db.commit()
_U1, _U2 = _u1.id, _u2.id


def _cria(wa, state):
    """Conversa V1 comum, com `state` escrito DIRETO no banco (a V1 nunca o escreve).
    `last_customer_msg_at` recebe o instante atual para que a janela de 24h
    (CONV-WINDOW-01) esteja aberta - sem isso, o passo 'mensagem' do ciclo V1
    seria rejeitado com 409 para as duas conversas por igual, o que provaria
    equivalencia mas nao o efeito real que a Task 0.3 pede."""
    conv = _Conversation(lead_id=1, whatsapp=wa, status="aberta", unread_count=0, is_bot_active=True)
    _db.add(conv)
    _db.commit()
    _db.execute(
        _sa_text("UPDATE conversations SET state=:s, state_version=7, closed_at=:c,"
                  " last_customer_msg_at=:lc WHERE id=:i"),
        {"s": state, "c": "2026-01-01 00:00:00",
         "lc": datetime.now(timezone.utc).isoformat(), "i": conv.id},
    )
    _db.commit()
    return conv.id


_main.app.dependency_overrides[get_current_user] = lambda: _u1
_client = TestClient(_main.app)

_OPERACIONAIS = ("is_bot_active", "atendente_id", "responsavel_id", "queued_at",
                 "primeira_resposta_humana_at", "status", "unread_count")


def _estado_operacional(cid):
    _db.expire_all()
    linha = _db.execute(
        _sa_text("SELECT " + ", ".join(_OPERACIONAIS) + " FROM conversations WHERE id=:i"),
        {"i": cid},
    ).fetchone()
    return dict(zip(_OPERACIONAIS, linha))


def _colunas_v2(cid):
    _db.expire_all()
    linha = _db.execute(
        _sa_text("SELECT state, state_version, closed_at FROM conversations WHERE id=:i"),
        {"i": cid},
    ).fetchone()
    return dict(zip(("state", "state_version", "closed_at"), linha))


def _ciclo_v1(cid):
    """A MESMA sequencia V1, na mesma ordem, para as duas linhas -
    listar, detalhe, claim, assign, release, enviar mensagem, encerrar (Task 0.3)."""
    passos = []
    passos.append(("listar", _client.get("/api/conversations").status_code))
    passos.append(("detalhe", _client.get(f"/api/conversations/{cid}").status_code))
    passos.append(("claim", _client.post(f"/api/conversations/{cid}/claim").status_code))
    passos.append(("assign", _client.post(f"/api/conversations/{cid}/assign",
                                          json={"user_id": _U2}).status_code))
    passos.append(("release", _client.post(f"/api/conversations/{cid}/release").status_code))
    passos.append(("mensagem", _client.post(f"/api/conversations/{cid}/messages",
                                            json={"content": "oi"}).status_code))
    passos.append(("encerrar", _client.put(f"/api/conversations/{cid}",
                                           json={"status": "encerrada"}).status_code))
    return passos


import datetime as _datetime_module  # noqa: E402
from unittest.mock import patch  # noqa: E402

_NOW_CONGELADO = datetime.now(timezone.utc)


class _RelogioCongelado(_datetime_module.datetime):
    """Subclasse real de datetime - so `.now()` e fixo; isoformat, aritmetica,
    comparacao e timezone continuam herdados e corretos."""

    @classmethod
    def now(cls, tz=None):
        return _NOW_CONGELADO if tz is not None else _NOW_CONGELADO.replace(tzinfo=None)


# --- B.2 - a API da V1 nao expoe os tres campos -----------------------------
# O alvo e o response_model REAL da rota: ConversationDetailWithWindow, declarada
# no proprio router. Mirar a classe-base provaria a propriedade sobre um objeto
# que a rota nao devolve.
_cid_b2 = _cria("5511900000002", "AI_TRIAGE")
_resp = _client.get(f"/api/conversations/{_cid_b2}")
check(_resp.status_code == 200, f"B.2 GET /{{id}} responde 200 (obteve {_resp.status_code})")
_json = _resp.json() if _resp.status_code == 200 else {}
for _campo in _COLUNAS_V2:
    check(_campo not in _json, f"B.2 o JSON da V1 NAO expoe a chave {_campo!r}")
check(
    _colunas_v2(_cid_b2)["state"] == "AI_TRIAGE",
    "B.2 pre-condicao real: a linha consultada TINHA state='AI_TRIAGE' (senao a prova e vazia)",
)

from app.routers.conversations import ConversationDetailWithWindow as _RespModel  # noqa: E402

check(
    not any(c in _RespModel.model_fields for c in _COLUNAS_V2),
    "B.2 o response_model da rota nao DECLARA nenhuma das tres colunas",
)

# --- B.3 - equivalencia operacional entre LEGACY e AI_TRIAGE ----------------
# Nao basta "nao quebrou": a V1 tem de produzir o MESMO resultado nas duas.
_cid_legacy = _cria("5511900000003", "LEGACY")
_cid_v2 = _cria("5511900000004", "AI_TRIAGE")

with patch("app.services.atendimento.datetime", _RelogioCongelado):
    _passos_legacy = _ciclo_v1(_cid_legacy)
    _passos_v2 = _ciclo_v1(_cid_v2)
check(_passos_legacy == _passos_v2,
      f"B.3 a mesma sequencia V1 devolve os MESMOS status HTTP ({_passos_legacy} vs {_passos_v2})")
check(all(200 <= c < 300 for _, c in _passos_legacy),
      f"B.3 pre-condicao: o ciclo V1 realmente rodou, sem erro ({_passos_legacy})")

_op_legacy = _estado_operacional(_cid_legacy)
_op_v2 = _estado_operacional(_cid_v2)
check(_op_legacy == _op_v2,
      f"B.3 os campos OPERACIONAIS terminam identicos ({_op_legacy} vs {_op_v2})")

# Os unicos campos autorizados a divergir sao os tres da V2.
check(_colunas_v2(_cid_legacy)["state"] == "LEGACY" and _colunas_v2(_cid_v2)["state"] == "AI_TRIAGE",
      "B.3 e `state` continua diferente entre as duas - a divergencia autorizada")

# --- B.4 - a V1 NUNCA incrementa state_version ------------------------------
# Prova executavel do risco R8. Falha de proposito se alguem "resolver" R8 sem
# desenho novo - nao e verificacao de saude, e trava.
_antes_v2 = _colunas_v2(_cid_v2)
check(_antes_v2["state_version"] == 7,
      f"B.4 pre-condicao: state_version era 7 antes do ciclo (obteve {_antes_v2['state_version']})")
_cid_b4 = _cria("5511900000005", "AI_TRIAGE")
_ciclo_v1(_cid_b4)
_depois = _colunas_v2(_cid_b4)
check(_depois["state_version"] == 7,
      f"B.4 claim+assign+release+mensagem+encerrar NAO incrementou state_version (R8) - obteve {_depois['state_version']}")
# Segundo envio, independente do que ja ocorreu dentro de _ciclo_v1 - reforca R8
# sobre uma mensagem adicional, nao repete a mesma prova.
_msg = _client.post(f"/api/conversations/{_cid_b4}/messages", json={"content": "oi"})
check(_colunas_v2(_cid_b4)["state_version"] == 7,
      "B.4 uma segunda mensagem pela V1 tambem nao incrementa state_version")

# --- B.5 - o CHECK e inerte para LEGACY -------------------------------------
_erro_legacy = None
try:
    _db.execute(_sa_text(
        "INSERT INTO conversations (lead_id, whatsapp, status, unread_count, is_bot_active,"
        " atendente_id, responsavel_id, queued_at, state)"
        " VALUES (1, '5511900000006', 'aberta', 0, 1, 1, 2, NULL, 'LEGACY')"))
    _db.commit()
except sqlalchemy.exc.IntegrityError as _exc:
    _db.rollback()
    _erro_legacy = str(_exc)
check(_erro_legacy is None,
      f"B.5 CHECK INERTE para LEGACY: at!=resp, bot ON, queued_at NULL e aceito ({_erro_legacy})")

# PROVA SEPARADA, e nao a mesma: o storage tolera `state` NULL por
# compatibilidade aditiva, e a MAQUINA V2 recusa essa linha. Sao duas
# propriedades diferentes - o banco nao sabe da regra, e B.5 nao prova a
# semantica de NULL (decisao 10 do subgate da Fase 4).
_erro_null = None
try:
    _db.execute(_sa_text(
        "INSERT INTO conversations (lead_id, whatsapp, status, unread_count, is_bot_active,"
        " atendente_id, responsavel_id, queued_at, state)"
        " VALUES (1, '5511900000007', 'aberta', 0, 1, 1, 2, NULL, NULL)"))
    _db.commit()
except sqlalchemy.exc.IntegrityError as _exc:
    _db.rollback()
    _erro_null = str(_exc)
check(_erro_null is None, f"B.5b o STORAGE aceita state NULL ({_erro_null})")

from app.v2.estados import Estado as _Estado, TransicaoInvalida as _TI, aplicar_transicao as _apl  # noqa: E402


class _Fake:
    state = None
    state_version = 0
    closed_at = None
    is_bot_active = True
    atendente_id = None
    responsavel_id = None
    queued_at = None
    primeira_resposta_humana_at = None
    status = "aberta"


_recusou = False
try:
    _apl(_Fake(), _Estado.AI_TRIAGE, motivo="prova_null")
except _TI as _exc:
    _recusou = "estado_atual_invalido" in str(_exc)
check(_recusou, "B.5c mas a MAQUINA V2 RECUSA state NULL (fail closed, decisao 10)")

_main.app.dependency_overrides.clear()
_db.close()


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
