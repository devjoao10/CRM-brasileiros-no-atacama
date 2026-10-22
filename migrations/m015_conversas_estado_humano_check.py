"""
BIA-V2 Fase 4 (CONVERSAS) - acrescenta o CHECK `ck_conversations_estado_humano`
a `conversations`.

    state IN (WAITING_HUMAN, HUMAN_ACTIVE)  =>  NOT is_bot_active
                                            AND atendente_id IS NOT NULL
                                            AND responsavel_id IS NOT NULL
                                            AND atendente_id = responsavel_id
                                            AND queued_at IS NOT NULL

Especificacao normativa: decisoes 5 e 12 do subgate da Fase 4, em
`docs/superpowers/plans/2026-08-29-bia-v2.md`. Sao exatamente CINCO condicoes -
`primeira_resposta_humana_at` e `status` NAO entram (o primeiro nem poderia:
os dois estados guardados exigem valores opostos e a guarda e comum aos dois).

DUAS COPIAS DELIBERADAS, SEM ACOPLAMENTO DE RUNTIME
A expressao abaixo e uma copia LOCAL. A outra vive em
`conversas/app/models/conversation.py`, no `CheckConstraint` do model. E
PROIBIDO importar uma da outra, importar de `app/v2/estados.py`, ou criar
modulo compartilhado so para hospedar esta string: migration nao importa model
neste repo (ver m003/m012/m013), e acoplar codigo de runtime a uma migration
custa mais que duas copias.

Isto NAO e "single source of truth" e nao deve ser descrito assim - existem
duas copias fisicas. A fonte normativa e a decisao 5 do plano; as duas
constantes sao replicas dela, e a protecao contra drift e um GATE EXECUTAVEL:
`tests/test_v2_m015_gate.py` compara as duas e falha se divergirem. Mudar esta
expressao sem espelhar no model quebra aquele teste de proposito.

NOT VALID + VALIDATE, E POR QUE
`ADD CONSTRAINT` sem `NOT VALID` valida as linhas existentes DENTRO da mesma
instrucao, segurando `ACCESS EXCLUSIVE` durante todo o scan - a tabela inteira
fica indisponivel. Com `NOT VALID` o lock forte dura so o tempo de gravar
metadado, e o `VALIDATE CONSTRAINT` posterior roda sob `SHARE UPDATE
EXCLUSIVE`, que nao bloqueia leitura nem escrita.

SQLITE: PULA COM SUCESSO
SQLite nao tem `ALTER TABLE ... ADD CONSTRAINT`, e banco novo ja recebe o
`CheckConstraint` pelo model via `create_all()` - nao ha nada a fazer, e
recusar transformaria "nada a fazer" em erro. Registra `skipped-sqlite` e sai
0. Divergencia deliberada do molde de m011/m012/m013, que RECUSAM SQLite.

RESSALVA que vem junto com essa escolha, e que o operador precisa saber: a
recusa daquelas migrations nunca foi sobre capacidade do SQLite - era protecao
contra `DATABASE_URL` apontando por engano para um arquivo local. Esse risco
vale igualmente aqui, e o skip o SILENCIA em vez de neutraliza-lo. Portanto:
**o exit 0 desta migration NAO e evidencia de que producao foi coberta.** O
runbook confirma a constraint no alvo real, por verificacao independente,
antes de considerar a Fase 4 aplicada.

Rollback: `DROP CONSTRAINT ck_conversations_estado_humano` - metadado,
instantaneo, sem perda de dado.

Roda em processo proprio do Conversas:

    DATABASE_URL=postgresql://... python migrations/m015_conversas_estado_humano_check.py

Aplicacao em PRODUCAO e acao humana com backup + aprovacao (ver
`migrations/README.md`). Agentes de IA NAO executam isto.
"""
import logging
import os
import sys

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger(__name__)


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `conversations`: banco errado, nao 'NO-OP'."""


_TABELA = "conversations"
_CONSTRAINT = "ck_conversations_estado_humano"

# COPIA LOCAL da expressao canonica da decisao 5. A outra copia esta no
# `CheckConstraint` de `conversas/app/models/conversation.py`. Ver docstring do
# modulo: a duplicacao e deliberada, e `tests/test_v2_m015_gate.py` e o gate
# que falha se as duas divergirem.
_CHECK_ESTADO_HUMANO_SQL = """
state NOT IN ('WAITING_HUMAN','HUMAN_ACTIVE')
OR (
    NOT is_bot_active
    AND atendente_id IS NOT NULL
    AND responsavel_id IS NOT NULL
    AND atendente_id = responsavel_id
    AND queued_at IS NOT NULL
)
"""


def normalizar_expressao(bruto):
    """
    Normalizacao MINIMA e DETERMINISTICA, para comparar as duas copias.

    Faz exatamente duas coisas: colapsa qualquer sequencia de espacos/quebras
    em UM espaco, e apara as bordas. Nada alem disso.

    O que ela deliberadamente NAO faz, porque igualaria expressoes
    logicamente diferentes: nao remove parenteses, nao reordena termos, nao
    normaliza caixa de identificadores nem de literais, nao trata `AND`/`OR`
    como comutativos. Uma normalizacao frouxa aqui destruiria o proprio gate -
    ela poderia declarar iguais duas expressoes que o banco avalia diferente.
    """
    if bruto is None:
        return None
    return " ".join(str(bruto).split())


def _constraint_existente(insp):
    """Devolve o dict da constraint pelo NOME, ou None. Idempotencia da decisao 12."""
    try:
        existentes = insp.get_check_constraints(_TABELA)
    except NotImplementedError:
        # Dialeto sem reflexao de CHECK - o caminho SQLite nem chega aqui.
        return None
    for constraint in existentes:
        if constraint.get("name") == _CONSTRAINT:
            return constraint
    return None


def _constraint_validada(engine):
    """
    True se `pg_constraint.convalidated` for True para `_CONSTRAINT`. So
    chamada com dialeto postgresql (SQLite nao tem esse catalogo, nem o
    conceito de `NOT VALID`).

    Existe porque uma reexecucao pode achar a constraint pelo NOME via
    `_constraint_existente` com o `VALIDATE CONSTRAINT` da execucao anterior
    incompleto - processo interrompido ENTRE as duas transacoes `engine.begin()`
    de `run()`. `NOT VALID` ja fica enforced para escrita nova, mas nunca foi
    provado contra as linhas pre-existentes. Sem esta checagem, `run()` trataria
    "constraint existe" como "constraint pronta" e reportaria OK sobre um estado
    que nunca verificou - exatamente o que o docstring de `run()` promete nao fazer.
    """
    with engine.connect() as conn:
        convalidated = conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = :nome"),
            {"nome": _CONSTRAINT},
        ).scalar()
    return bool(convalidated)


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta RuntimeError se o mundo nao
    ficou como deveria depois do DDL — nunca engole nada, nunca imprime OK
    sobre um estado que nao verificou.

    `actions` e recebida de fora de proposito: se isto levantar no meio, o
    chamador ainda precisa saber QUAIS objetos ja foram aplicados.
    """
    if engine is None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL nao definida — este script nao adivinha alvo. "
                "Passe um engine ou exporte a variavel."
            )
        engine = create_engine(url)

    actions = [] if actions is None else actions
    insp = inspect(engine)

    if _TABELA not in insp.get_table_names():
        raise WrongTargetError(
            "[m015] RECUSADO — o alvo nao tem a tabela `conversations`.\n"
            "[m015] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m015] precisa deste script. Banco EXISTENTE sem essa tabela quer dizer\n"
            "[m015] ALVO ERRADO (DATABASE_URL para outra base, nome trocado, replica\n"
            "[m015] vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
        )

    dialect = engine.dialect.name

    if dialect == "sqlite":
        # Ver docstring do modulo: nao ha ALTER ... ADD CONSTRAINT em SQLite, e
        # banco novo ja recebe o CHECK pelo model. Sai 0 — mas o exit 0 NAO e
        # evidencia de cobertura de producao.
        actions.append("skipped-sqlite")
        return actions

    ja_existia = _constraint_existente(insp) is not None
    if ja_existia:
        actions.append(f"{_CONSTRAINT}:already-present")
        # Reexecucao: a constraint existe pelo NOME, mas o VALIDATE CONSTRAINT
        # da execucao anterior pode ter sido interrompido antes de completar
        # (ver `_constraint_validada`). Completa-lo aqui e o que sustenta a
        # promessa do docstring acima - nunca reportar OK sobre um estado que
        # nao foi verificado.
        if dialect == "postgresql" and not _constraint_validada(engine):
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {_TABELA} VALIDATE CONSTRAINT {_CONSTRAINT}"))
            actions.append(f"{_CONSTRAINT}:validated")
    else:
        expressao = normalizar_expressao(_CHECK_ESTADO_HUMANO_SQL)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {_TABELA} ADD CONSTRAINT {_CONSTRAINT} "
                    f"CHECK ({expressao}) NOT VALID"
                )
            )
        actions.append(f"{_CONSTRAINT}:added (NOT VALID)")

        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {_TABELA} VALIDATE CONSTRAINT {_CONSTRAINT}"))
        actions.append(f"{_CONSTRAINT}:validated")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova NOME **e** EXPRESSAO, nunca so existencia.

    Checar so o nome deixaria passar uma constraint `ck_conversations_estado_humano`
    com expressao diferente da decisao 5 — aplicada a mao num reparo, ou
    sobrevivente de uma versao anterior do texto, que a checagem de idempotencia
    por nome nunca substituiria. E o mesmo defeito que o gate da m013 ja rejeita
    para indices: "nome certo, coluna errada" tem de falhar como se o objeto
    estivesse ausente.

    A comparacao usa `normalizar_expressao`, que so colapsa whitespace. O
    PostgreSQL reescreve a expressao ao armazena-la (`pg_get_constraintdef`
    reformata parenteses, `NOT IN`, casts), entao a comparacao textual estrita
    pode acusar diferenca que nao e semantica — por isso o gate compara os
    TOKENS relevantes (colunas e operadores citados), nao a string inteira. O
    que garante que as duas COPIAS DE CODIGO nao divergem e o teste cruzado em
    `tests/test_v2_m015_gate.py`, que compara fonte contra fonte, sem banco.
    """
    insp_pos = inspect(engine)
    constraint = _constraint_existente(insp_pos)
    if constraint is None:
        raise RuntimeError(
            f"{_CONSTRAINT} AUSENTE depois do DDL — migration abortada."
        )
    actions.append(f"{_CONSTRAINT}:presente")

    refletida = normalizar_expressao(constraint.get("sqltext"))
    if not refletida:
        raise RuntimeError(
            f"{_CONSTRAINT} existe mas o catalogo nao devolveu a expressao — "
            "impossivel provar que ela e a da decisao 5. Migration abortada."
        )

    # Tokens que a expressao da decisao 5 obriga. Comparar a string inteira
    # falharia por reformatacao do PostgreSQL, nao por divergencia real.
    obrigatorios = (
        "WAITING_HUMAN",
        "HUMAN_ACTIVE",
        "is_bot_active",
        "atendente_id",
        "responsavel_id",
        "queued_at",
    )
    faltando = [t for t in obrigatorios if t not in refletida]
    if faltando:
        raise RuntimeError(
            f"{_CONSTRAINT} existe mas a expressao no catalogo nao cita {faltando} — "
            f"nome bate, expressao nao bate. Expressao refletida: {refletida!r}. "
            "Migration abortada."
        )
    actions.append(f"{_CONSTRAINT}:expressao-verificada")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais.

    Diferente de m011/m012/m013, esta migration NAO recusa SQLite — ela pula
    com sucesso (decisao 12). A ressalva que vem junto esta no docstring do
    modulo: o exit 0 nao prova cobertura de producao.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None, (
            "[m015] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m015] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m015]   DATABASE_URL=postgresql://... python migrations/m015_conversas_estado_humano_check.py"
        )
    return url, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m015] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m015]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m015] falha inesperada")
        print(f"[m015] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if "skipped-sqlite" in actions:
        print("[m015] OK — pulada em SQLite (o CHECK vem do model via create_all)")
        print("[m015] ATENCAO: este exit 0 NAO e evidencia de que producao foi coberta.")
    elif any(":added" in a for a in actions):
        print("[m015] OK — constraint aplicada e validada")
    else:
        print("[m015] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
