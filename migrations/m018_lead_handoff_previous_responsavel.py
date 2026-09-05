"""
m018 — BIA-V2 R15-R17 (CRM): coluna `previous_responsavel_id` em
`lead_handoff_operations`.

Acrescenta (aditivo, sem destruir nada):
    lead_handoff_operations.previous_responsavel_id  INTEGER  NULL  (sem default)

Especificacao normativa: secao 8 ("Migrations") de
`bia-v2-r15-r17-design-v4.md`. Nada aqui e escolha deste arquivo; tudo vem
de la. A tabela em si (e o UNIQUE em `event_id`) ja existe desde a m017
(Fase 6) — esta migration so acrescenta UMA coluna a ela.

O QUE A COLUNA GUARDA
----------------------
O valor de `leads.responsavel_id` no INSTANTE em que a linha do livro-razao
foi gravada — "de quem" o lead saiu, ao lado de `target_user_id` ("para
quem" ele foi). Ate aqui essa informacao so existia em `lead_history`
(coluna `responsavel_id` do evento `responsavel_changed`), presa a uma
regra de escrita diferente (so grava quando o valor MUDA — ver a secao
"POR QUE TABELA PROPRIA" no docstring do model). Guardar o "de quem"
tambem no livro-razao permite comparar o estado pre-handoff sem juntar as
duas tabelas por `lead_id`+janela de tempo, o que seria fragil sob
concorrencia.

POR QUE NULL SIMPLES, SEM DEFAULT (diferente da m014)
-------------------------------------------------------
A m014 (Conversas) precisa de `DEFAULT 'LEGACY'` porque toda linha JA
EXISTENTE de `conversations` e uma linha V1 que a V2 precisa reconhecer como
tal. Aqui nao ha equivalente: `lead_handoff_operations` so existe desde a
m017 (Fase 6, BIA-V2), entao toda linha que ja existir antes desta migration
foi escrita pelo endpoint SEM esta coluna — o valor correto para ela e "nao
sabemos o responsavel anterior", que e exatamente o que NULL representa, nao
um estado a reparar. Novas linhas, escritas pelo endpoint a partir da Fase
R15-R17, preenchem o valor explicitamente. Por isso este script nao faz
backfill (ao contrario da m014): nao ha valor correto a escrever nas linhas
antigas, so a ausencia dele.

`INTEGER` sem mapa por dialeto: ao contrario de TIMESTAMP (m012/m013/m014),
uma coluna INTEGER nullable sem default tem sintaxe IDENTICA em SQLite e
PostgreSQL — mesmo padrao ja usado em `lead_id`/`target_user_id` na m017.

SEM ForeignKey, DE PROPOSITO
------------------------------
Mesma razao de `lead_id`/`target_user_id` (m017) e de `conversation_events`
(m013): o livro-razao e append-only e precisa sobreviver a delecao do
usuario que descreve. Uma FK em `users.id` apagaria (ou bloquearia a
delecao de) historico de auditoria por causa de uma linha antiga. A
existencia do usuario e verificada pelo endpoint, nao pelo schema.

RECUSA COM `WrongTargetError` SE A TABELA NAO EXISTIR
--------------------------------------------------------
Ao contrario da m017 (que checa `leads`, a tabela-pai que precisa existir
ANTES de criar `lead_handoff_operations`), esta migration ALTERA
`lead_handoff_operations` diretamente — entao o alvo certo para o gate e a
propria tabela que recebe a coluna, mesmo padrao da m014 (que checa
`conversations`, nao alguma tabela anterior na cadeia). Banco existente sem
`lead_handoff_operations` quer dizer banco errado OU a m017 ainda nao foi
aplicada ali — nunca "nada a fazer".

COMPATIBILIDADE COM O QUE JA EXISTE
--------------------------------------
Total. A coluna e nullable, sem default, e nenhum codigo anterior a esta
Fase a le ou escreve — linhas antigas continuam validas com
`previous_responsavel_id IS NULL`. Rollback: deixar sem uso. NUNCA
`DROP COLUMN` (padrao do projeto, ver `migrations/README.md`).

ESTA MIGRATION E DO CRM, NAO DO CONVERSAS: como a m017 (e ao contrario da
m013/m014), ela NAO insere `conversas/` no `sys.path` e nao importa
`app.*` — resolve o alvo so pela env var.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do CRM.

Uso (LOCAL / STAGING):
    DATABASE_URL=sqlite:///./crm.db python migrations/m018_lead_handoff_previous_responsavel.py --allow-sqlite

Uso (PRODUCAO):
    DATABASE_URL=postgresql://... python migrations/m018_lead_handoff_previous_responsavel.py

Codigos de saida: 0 = aplicado/no-op | 1 = recusa ou falha inesperada

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import os
import sys

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrations.m018")


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `lead_handoff_operations`: banco errado, nao 'NO-OP'."""


_TABELA = "lead_handoff_operations"
_COLUNA = "previous_responsavel_id"

# Espelha a coluna do model — usado SO pelo gate pos-DDL, para provar
# presenca (nao tipo: ver `_verificar_pos_ddl`).
_COLUNAS_OBRIGATORIAS = (_COLUNA,)


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta `WrongTargetError` (alvo nao
    e este banco) ou `RuntimeError` (o mundo nao ficou como deveria depois do
    DDL) — nunca engole nada, nunca imprime OK sobre estado que nao
    verificou.

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
        # Mesmo gate da m011/m013/m014: banco NOVO nasce completo pelo
        # create_all() e nao precisa deste script; banco EXISTENTE sem
        # `lead_handoff_operations` quer dizer ALVO ERRADO (ou a m017 ainda
        # nao foi aplicada ali), nao "nada a fazer".
        raise WrongTargetError(
            f"[m018] RECUSADO — o alvo nao tem a tabela `{_TABELA}`.\n"
            "[m018] Banco NOVO nasce completo pelo create_all() do CRM e nao\n"
            "[m018] precisa deste script. Banco EXISTENTE sem essa tabela quer\n"
            "[m018] dizer ALVO ERRADO (DATABASE_URL para outra base, nome trocado,\n"
            "[m018] replica vazia) ou que a m017 ainda nao foi aplicada aqui.\n"
            "[m018] Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
        )

    existentes = {c["name"] for c in insp.get_columns(_TABELA)}

    with engine.begin() as conn:
        if _COLUNA in existentes:
            actions.append(f"{_COLUNA}:already-present")
        else:
            conn.execute(text(f"ALTER TABLE {_TABELA} ADD COLUMN {_COLUNA} INTEGER"))
            actions.append(f"{_COLUNA}:added (INTEGER)")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova a garantia que `run()` promete, por REINSPECAO — nunca lendo de
    volta o que o proprio DDL declarou.

    `ALTER TABLE ... ADD COLUMN` nao tem variante `IF NOT EXISTS` portavel
    entre SQLite e PostgreSQL (por isso o `if _COLUNA in existentes` acima),
    entao aqui a checagem e simples: a coluna existe pelo NOME depois do
    DDL. Nunca tenta provar equivalencia de TIPO entre SQLite e PostgreSQL
    (mesma fronteira que m013/m014/m017 ja declaram) e nao ha backfill para
    verificar: a coluna nao tem DEFAULT, entao toda linha pre-existente FICA
    NULL de proposito — esse e o estado correto, nao uma falha a reparar.

    Extraida de `run()` para poder ser exercida sozinha contra um SQLite
    descartavel, sem nunca chamar `run()`/`main()`.
    """
    insp_pos = inspect(engine)
    if _TABELA not in insp_pos.get_table_names():
        raise RuntimeError(f"{_TABELA} AUSENTE depois do DDL — migration abortada.")

    colunas_pos = {c["name"] for c in insp_pos.get_columns(_TABELA)}
    faltando = [c for c in _COLUNAS_OBRIGATORIAS if c not in colunas_pos]
    if faltando:
        raise RuntimeError(
            f"colunas ausentes em {_TABELA} depois do DDL: {faltando} — coluna "
            "pre-existente/parcial (ALTER foi pulado por 'already-present' contra "
            "um schema incompleto). Migration abortada."
        )
    actions.append("colunas:verificadas")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de `app.config`: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel (defeito real de m005/m008/m009).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url:
        return None, allow_sqlite, (
            "[m018] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m018] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m018]   DATABASE_URL=postgresql://... python migrations/m018_lead_handoff_previous_responsavel.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m018] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m018] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m018] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m018] alvo (crm): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m018]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m018] falha inesperada")
        print(f"[m018] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":added" in a for a in actions):
        print("[m018] OK — coluna aplicada (idempotente)")
    else:
        print("[m018] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
