"""
BIA-V2 R8/R10 - barreira de escrita operacional (`conversas/app/v2/compat.py`).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md (R8, R10, P6) e
bia-v2-r8-r10-inventory.md (secoes 0, 3.2, 3.3).

O QUE ESTE ARQUIVO PROVA
-------------------------
A barreira intercepta no `before_flush` da `SessionLocal`, entao ela e provada
como se prova um invariante de banco: escrevendo pela V1 (mutacao direta dos
campos operacionais, exatamente como `aplicar_estado_humano` e o `PUT /{id}`
fazem) e observando o que acontece no flush. Nenhum dos oito arquivos
protegidos e importado por este teste NEM pelo modulo sob teste.

  A. PASSTHROUGH - `LEGACY`, `AI_TRIAGE`, `NULL` e `CLOSED` ficam intactos.
     E o que mantem a Familia B.3/B.4 de `test_v2_v1_intacta.py` verde, e o que
     registra `AI_TRIAGE` como residual ACEITO desta rodada, nao como esquecido.
  B. INERCIA - escrita que nao toca campo da maquina (`unread_count`) nao
     dispara transicao nenhuma, nem em conversa sob barreira.
  C. RECUSA - as quatro classes de escrita V1 incoerente em
     `WAITING_HUMAN`/`HUMAN_ACTIVE` (propriedades 2, 3, 4 do enunciado).
  D. NORMALIZACAO - a escrita V1 COERENTE passa e leva `state`/`state_version`/
     `closed_at` junto, na MESMA transacao (propriedades 5 e 6).
  E. NAO-REENTRANCIA - a escrita da propria V2 (via `aplicar_transicao`) nao e
     interceptada: `state_version` anda UMA vez, nunca duas.
  F. SEM DEPENDENCIA CIRCULAR - `compat.py` nao importa nenhum dos oito
     arquivos protegidos (propriedade 10).
  G. REGISTRO NOS TRES ENTRYPOINTS - main.py e os dois workers de processo
     separado (`worker_reconciliacao.py`, `worker_retomada.py`) importam
     `app.v2.compat`; sem isso a barreira so existe no processo da API, e os
     workers escrevem campo operacional sem rede de seguranca alguma.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_compat_barreira.py
"""
import ast
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_compat_barreira_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query - mesmo padrao de tests/test_v2_gate.py.
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.v2 import compat  # noqa: E402
from app.v2.estados import Estado, TransicaoInvalida, aplicar_transicao  # noqa: E402

Base.metadata.create_all(bind=engine, tables=[Conversation.__table__, ConversationEvent.__table__])
db = SessionLocal()

_contador_wa = iter(range(1, 10000))
_AGORA = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def criar(**campos):
    """Linha crua, com os campos exatamente como pedidos - sem passar pela maquina."""
    base = dict(
        lead_id=1,
        whatsapp=f"5511911{next(_contador_wa):06d}",
        status="aberta",
        unread_count=0,
        is_bot_active=True,
        atendente_id=None,
        responsavel_id=None,
        queued_at=None,
        primeira_resposta_humana_at=None,
        state="LEGACY",
        state_version=0,
        closed_at=None,
    )
    base.update(campos)
    conv = Conversation(**base)
    db.add(conv)
    db.commit()
    return conv


def humana(estado, *, dono=7, versao=3):
    """Conversa COERENTE em WAITING_HUMAN ou HUMAN_ACTIVE (as sete invariantes)."""
    return criar(
        state=estado,
        state_version=versao,
        is_bot_active=False,
        atendente_id=dono,
        responsavel_id=dono,
        queued_at=_AGORA,
        primeira_resposta_humana_at=_AGORA if estado == "HUMAN_ACTIVE" else None,
        status="aberta",
    )


def retrato(conv):
    return (conv.state, conv.state_version, conv.closed_at)


def escrever_v1(conv, **campos):
    """
    Escrita da V1: muta campos operacionais e commita. NUNCA toca state,
    state_version nem closed_at - e o que a Familia B.1 prova sobre os oito
    arquivos protegidos, reproduzido aqui.
    """
    for campo, valor in campos.items():
        setattr(conv, campo, valor)
    db.commit()


def recusa_de(conv, **campos):
    """Executa a escrita V1 esperando `TransicaoInvalida`; devolve a excecao ou None."""
    try:
        escrever_v1(conv, **campos)
    except TransicaoInvalida as exc:
        db.rollback()
        return exc
    return None


# ===========================================================================
# A. PASSTHROUGH - fora de WAITING_HUMAN/HUMAN_ACTIVE a barreira e inerte
# ===========================================================================
print("\n--- A: PASSTHROUGH (LEGACY / AI_TRIAGE / NULL / CLOSED) ---")

for estado in ("LEGACY", "AI_TRIAGE", None, "CLOSED"):
    conv = criar(
        state=estado,
        state_version=5,
        is_bot_active=True,
        closed_at=_AGORA if estado == "CLOSED" else None,
    )
    antes = retrato(conv)
    # O ciclo V1 inteiro, de uma vez: assumir, sair da fila, responder, encerrar.
    escrever_v1(
        conv,
        atendente_id=1,
        responsavel_id=2,          # DIFERENTE de atendente_id de proposito
        is_bot_active=False,
        queued_at=_AGORA,
        primeira_resposta_humana_at=_AGORA,
        status="encerrada",
    )
    check(
        retrato(conv) == antes,
        f"A. state={estado!r}: o ciclo V1 completo NAO tocou "
        f"state/state_version/closed_at (antes={antes}, depois={retrato(conv)})",
    )
    check(
        conv.atendente_id == 1 and conv.responsavel_id == 2 and conv.status == "encerrada",
        f"A. state={estado!r}: e a escrita V1 foi APLICADA como sempre foi "
        f"(at={conv.atendente_id}, resp={conv.responsavel_id}, status={conv.status!r})",
    )

# ===========================================================================
# B. INERCIA - escrita sem campo da maquina nao dispara transicao
# ===========================================================================
print("\n--- B: INERCIA (escrita que nao e de estado) ---")

conv = humana("WAITING_HUMAN")
antes = retrato(conv)
conv.unread_count = 9
db.commit()
check(
    retrato(conv) == antes,
    f"B. `unread_count` (marcar como lido) NAO incrementa state_version "
    f"(antes={antes}, depois={retrato(conv)})",
)
check(conv.unread_count == 9, "B. e a escrita de unread_count foi aplicada")

conv2 = humana("WAITING_HUMAN", dono=4)
antes2 = retrato(conv2)
conv2.atendente_id = 4  # MESMO valor: reatribuicao sem mudanca
db.commit()
check(
    retrato(conv2) == antes2,
    f"B. reatribuir o MESMO valor a um campo operacional tambem nao transiciona "
    f"(antes={antes2}, depois={retrato(conv2)})",
)

# ===========================================================================
# C. RECUSA - propriedades 2, 3 e 4 do enunciado
# ===========================================================================
print("\n--- C: RECUSA de escrita V1 incoerente sob a V2 ---")

# C1 (propriedade 2) - roubar o dono operacional sem levar o comercial junto.
conv = humana("WAITING_HUMAN", dono=7)
exc = recusa_de(conv, atendente_id=8)
check(
    exc is not None and "invariante_destino" in str(exc),
    f"C1 (prop.2) WAITING_HUMAN: trocar so `atendente_id` (7->8), deixando "
    f"`responsavel_id`=7, e RECUSADO [{exc}]",
)
db.refresh(conv)
check(
    (conv.atendente_id, conv.responsavel_id, conv.state_version) == (7, 7, 3),
    f"C1 (prop.2) e NADA foi persistido - o dono V2 continua de pe "
    f"(at={conv.atendente_id}, resp={conv.responsavel_id}, v={conv.state_version})",
)

# C2 (propriedade 3) - religar a BIA por cima de um atendimento humano.
conv = humana("HUMAN_ACTIVE", dono=7)
exc = recusa_de(conv, is_bot_active=True)
check(
    exc is not None and "invariante_destino" in str(exc),
    f"C2 (prop.3) HUMAN_ACTIVE: ligar `is_bot_active` deixando o humano dono "
    f"e RECUSADO [{exc}]",
)
db.refresh(conv)
check(
    conv.is_bot_active is False and conv.state_version == 3,
    f"C2 (prop.3) e a BIA continua desligada (bot={conv.is_bot_active}, "
    f"v={conv.state_version})",
)

# C3 (propriedade 4) - release zerando o dono no meio do atendimento.
conv = humana("WAITING_HUMAN", dono=7)
exc = recusa_de(conv, atendente_id=None)
check(
    exc is not None and "invariante_destino" in str(exc),
    f"C3 (prop.4) WAITING_HUMAN: zerar `atendente_id` (release) e RECUSADO [{exc}]",
)
db.refresh(conv)
check(
    conv.atendente_id == 7 and conv.state_version == 3,
    f"C3 (prop.4) e o dono sobreviveu (at={conv.atendente_id}, v={conv.state_version})",
)

# C4 (propriedade 4, lado comercial) - a cadeia de dano do _repair_responsavel_cache.
conv = humana("HUMAN_ACTIVE", dono=7)
exc = recusa_de(conv, responsavel_id=99)
check(
    exc is not None and "invariante_destino" in str(exc),
    f"C4 (prop.4) HUMAN_ACTIVE: divergir `responsavel_id` de `atendente_id` "
    f"e RECUSADO [{exc}]",
)
db.refresh(conv)
check(conv.responsavel_id == 7, f"C4 (prop.4) e o cache nao divergiu (resp={conv.responsavel_id})")

# C5 - aresta inexistente: HUMAN_ACTIVE nao volta para WAITING_HUMAN.
conv = humana("HUMAN_ACTIVE", dono=7)
exc = recusa_de(conv, primeira_resposta_humana_at=None)
check(
    exc is not None and "aresta_nao_permitida" in str(exc),
    f"C5 HUMAN_ACTIVE -> WAITING_HUMAN nao e aresta do grafo, e a barreira usa o "
    f"grafo da maquina em vez de reimplementa-lo [{exc}]",
)

# ===========================================================================
# D. NORMALIZACAO - propriedades 5 e 6
# ===========================================================================
print("\n--- D: NORMALIZACAO da escrita V1 COERENTE ---")

# D1 - primeira resposta humana: WAITING_HUMAN -> HUMAN_ACTIVE.
conv = humana("WAITING_HUMAN", dono=7, versao=3)
escrever_v1(conv, primeira_resposta_humana_at=_AGORA)
check(
    (conv.state, conv.state_version) == ("HUMAN_ACTIVE", 4),
    f"D1 (prop.6) a 1a resposta humana leva WAITING_HUMAN -> HUMAN_ACTIVE e "
    f"incrementa state_version 3->4 (obteve {conv.state!r}, {conv.state_version})",
)

# D2 (propriedade 5) - encerramento V1 passa a gravar closed_at e state.
conv = humana("HUMAN_ACTIVE", dono=7, versao=3)
escrever_v1(conv, status="encerrada")
check(
    conv.state == "CLOSED" and conv.state_version == 4 and conv.closed_at is not None,
    f"D2 (prop.5) `status='encerrada'` pela V1 move state para CLOSED, incrementa "
    f"a versao E grava closed_at (state={conv.state!r}, v={conv.state_version}, "
    f"closed_at={conv.closed_at!r}) - era exatamente o furo da L2051 do plano",
)

# D3 (propriedade 3, lado positivo) - devolver a conversa INTEIRA para a BIA
# e coerente, e a maquina tem a aresta para isso.
conv = humana("WAITING_HUMAN", dono=7, versao=3)
escrever_v1(
    conv,
    is_bot_active=True,
    atendente_id=None,
    queued_at=None,
    primeira_resposta_humana_at=None,
)
check(
    (conv.state, conv.state_version) == ("AI_TRIAGE", 4),
    f"D3 (prop.3) o toggle 'religar a BIA' COMPLETO (os quatro campos) e aceito e "
    f"vira AI_TRIAGE (obteve {conv.state!r}, {conv.state_version}) - a barreira "
    f"bloqueia o incoerente (C2), nao a operacao legitima",
)

# D4 - reatribuicao coerente (atendente E responsavel juntos): self-transition.
conv = humana("WAITING_HUMAN", dono=7, versao=3)
escrever_v1(conv, atendente_id=8, responsavel_id=8)
check(
    (conv.state, conv.state_version, conv.atendente_id) == ("WAITING_HUMAN", 4, 8),
    f"D4 (prop.2) trocar os DOIS donos juntos e coerente: aplica e versiona "
    f"(state={conv.state!r}, v={conv.state_version}, at={conv.atendente_id})",
)

# D5 (propriedade 6, a prova forte) - o incremento vai na MESMA transacao.
conv = humana("WAITING_HUMAN", dono=7, versao=3)
conv.primeira_resposta_humana_at = _AGORA
db.flush()  # SEM commit
check(
    (conv.state, conv.state_version) == ("HUMAN_ACTIVE", 4),
    f"D5 (prop.6) apos o FLUSH (antes de qualquer commit) o estado ja andou "
    f"junto com o campo operacional (obteve {conv.state!r}, {conv.state_version})",
)
db.rollback()
db.refresh(conv)
check(
    (conv.state, conv.state_version, conv.primeira_resposta_humana_at) == ("WAITING_HUMAN", 3, None),
    f"D5 (prop.6) e um rollback desfaz AS DUAS ESCRITAS JUNTAS - nao existe "
    f"transacao separada para o estado (obteve {conv.state!r}, {conv.state_version}, "
    f"{conv.primeira_resposta_humana_at!r})",
)

# ===========================================================================
# E. NAO-REENTRANCIA - a escrita da propria V2 nao e interceptada
# ===========================================================================
print("\n--- E: NAO-REENTRANCIA (a V2 nao passa pela barreira) ---")

conv = humana("WAITING_HUMAN", dono=7, versao=3)
conv.primeira_resposta_humana_at = _AGORA
aplicar_transicao(conv, Estado.HUMAN_ACTIVE, motivo="teste_v2")
db.commit()
check(
    conv.state_version == 4,
    f"E. quando a V2 chama `aplicar_transicao` explicitamente, a barreira NAO "
    f"transiciona de novo: state_version anda UMA vez (3->{conv.state_version}, "
    f"nao 3->5)",
)
check(conv.state == "HUMAN_ACTIVE", f"E. e o destino e o que a V2 pediu ({conv.state!r})")

# ===========================================================================
# F. SEM DEPENDENCIA CIRCULAR (propriedade 10)
# ===========================================================================
print("\n--- F: SEM DEPENDENCIA CIRCULAR com os oito arquivos protegidos ---")

_PROTEGIDOS_MODULOS = (
    "app.services.atendimento",
    "app.services.outbound",
    "app.routers.webhook",
    "app.routers.conversations",
    "app.services.crm",
)
_fonte = (CONVERSAS_DIR / "app" / "v2" / "compat.py").read_text(encoding="utf-8")
_importados = set()
for _no in ast.walk(ast.parse(_fonte)):
    if isinstance(_no, ast.Import):
        _importados.update(a.name for a in _no.names)
    elif isinstance(_no, ast.ImportFrom) and _no.module:
        _importados.add(_no.module)

check(
    not (_importados & set(_PROTEGIDOS_MODULOS)),
    f"F. (prop.10) compat.py nao importa NENHUM arquivo protegido "
    f"(importa: {sorted(_importados)})",
)
check(
    "app.v2.compat" not in _fonte.replace("app/v2/compat.py", ""),
    "F. (prop.10) e nenhum arquivo protegido precisa importar compat.py - "
    "a barreira e do lado do flush, nao do call site",
)
check(
    compat.ESTADOS_SOB_BARREIRA == frozenset({"WAITING_HUMAN", "HUMAN_ACTIVE"}),
    f"F. o escopo declarado e EXATAMENTE os dois estados pos-handoff "
    f"({sorted(compat.ESTADOS_SOB_BARREIRA)}) - `AI_TRIAGE` fica fora por "
    f"decisao registrada, nao por esquecimento",
)

# ===========================================================================
# G. REGISTRO NOS TRES ENTRYPOINTS DE PROCESSO
# ===========================================================================
print("\n--- G: os tres entrypoints importam app.v2.compat (registram a barreira) ---")


def _importa_compat(caminho: pathlib.Path) -> bool:
    """
    `True` se `caminho` tem `from app.v2 import compat` (ou `import app.v2.compat`)
    em algum lugar do modulo - AST, mesmo metodo da secao F, so que na direcao
    inversa: aqui se prova que o import EXISTE, la se prova que ele NAO existe.
    """
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    for no in ast.walk(arvore):
        if isinstance(no, ast.ImportFrom) and no.module == "app.v2":
            if any(alias.name == "compat" for alias in no.names):
                return True
        if isinstance(no, ast.Import) and any(alias.name == "app.v2.compat" for alias in no.names):
            return True
    return False


# `event.listens_for` registra o listener em MEMORIA DO PROCESSO - cada um destes
# tres roda como processo OS separado (`main.py` e a API; os outros dois sao os
# workers `python -m app.v2.worker_*`), entao o import precisa existir EM CADA
# UM. Faltar num worker nao quebra nada visivelmente hoje (os dois so escrevem
# campo operacional na MESMA chamada que ja aplica `aplicar_transicao`), mas
# remove a rede de seguranca contra uma escrita futura que esqueca de parear
# os dois - exatamente o gap que esta secao existe para nao deixar regredir.
_ENTRYPOINTS_BARREIRA = (
    CONVERSAS_DIR / "app" / "main.py",
    CONVERSAS_DIR / "app" / "v2" / "worker_reconciliacao.py",
    CONVERSAS_DIR / "app" / "v2" / "worker_retomada.py",
)

for _entrypoint in _ENTRYPOINTS_BARREIRA:
    check(
        _importa_compat(_entrypoint),
        f"G. {_entrypoint.relative_to(ROOT).as_posix()} importa app.v2.compat "
        f"(registra a barreira before_flush neste processo)",
    )

db.close()

print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("Todas as verificacoes passaram.")
