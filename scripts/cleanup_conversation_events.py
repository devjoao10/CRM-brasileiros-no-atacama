"""
scripts/cleanup_conversation_events.py

WP R13 — retencao/limpeza de `conversation_events` (Conversas).

`conversation_events` (conversas/app/models/evento.py) e uma trilha de
auditoria APPEND-ONLY: o fluxo normal do app nunca faz UPDATE/DELETE nela.
Este script e a UNICA excecao deliberada — uma ferramenta operacional, rodada
a mao (nunca pelo lifespan do app, nunca por migration), para aplicar a
retencao configurada em `CONVERSATION_EVENTS_RETENTION_DAYS`
(conversas/app/config.py).

MODO DEFAULT E DRY RUN: sem `--apply`, o script SO relata (cutoff, contagem
elegivel, contagem por event_type) e nunca deleta nada. Deletar de verdade
exige DUAS flags explicitas — `--apply` E `--confirm` — porque nao existe
ainda nenhuma convencao de flag destrutiva neste repo (scripts/ e
migrations/ nao tem precedente) e a tabela e uma trilha de auditoria: o
custo de um "oops" aqui e alto.

NUNCA imprime conteudo de linha (payload, whatsapp_msg_id ou qualquer outro
campo potencialmente sensivel) — nem em dry run, nem em apply. So
identificadores tecnicos/agregados: cutoff, contagens totais e por
event_type.

Deleta em LOTES (id ascendente, LIMIT por lote) para nunca carregar todas as
linhas elegiveis em memoria de uma vez; cada lote roda e commita como uma
transacao propria — interromper o script no meio deixa os lotes ja aplicados
aplicados e o restante elegivel fica para a proxima execucao (o filtro e
sempre `created_at < cutoff`, entao rodar de novo e seguro).

Uso:
    python scripts/cleanup_conversation_events.py                    # dry run (default)
    python scripts/cleanup_conversation_events.py --apply             # RECUSA, nao deleta (falta --confirm)
    python scripts/cleanup_conversation_events.py --apply --confirm   # deleta de verdade, em lotes
"""
import argparse
import logging
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# `app.*` deve resolver para conversas/app (mesmo padrao de
# migrations/m013_conversation_events.py e scripts/smoke_seed_conversas.py).
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import CONVERSATION_EVENTS_RETENTION_DAYS  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402

logger = logging.getLogger("scripts.cleanup_conversation_events")

DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class ResultadoLimpeza:
    """Resultado agregado de uma rodada — nunca carrega conteudo de linha."""

    cutoff: datetime
    elegivel_total: int
    por_tipo: list[tuple[str, int]]
    deletado_total: int
    aplicado: bool


def calcular_cutoff(retention_days: int, *, now: datetime | None = None) -> datetime:
    """
    Instante (UTC) abaixo do qual um evento e elegivel para limpeza.

    `retention_days < 0` e recusado: geraria um cutoff no FUTURO e tornaria
    elegivel praticamente todo o historico (inclusive linhas recem-gravadas)
    — o oposto de retencao segura.

    `now` e injetavel para teste deterministico da borda do cutoff — mesmo
    padrao de `service_window_open` (conversas/app/models/conversation.py).
    """
    if retention_days < 0:
        raise ValueError(
            f"CONVERSATION_EVENTS_RETENTION_DAYS invalido: {retention_days} (precisa ser >= 0)"
        )
    agora = now if now is not None else datetime.now(timezone.utc)
    return agora - timedelta(days=retention_days)


def _contagem_por_tipo(db: Session, cutoff: datetime) -> list[tuple[str, int]]:
    """GROUP BY event_type — agregado, nunca traz linha nem payload."""
    linhas = (
        db.query(ConversationEvent.event_type, func.count(ConversationEvent.id))
        .filter(ConversationEvent.created_at < cutoff)
        .group_by(ConversationEvent.event_type)
        .order_by(ConversationEvent.event_type)
        .all()
    )
    return [(tipo, int(total)) for tipo, total in linhas]


def limpar_eventos_antigos(
    db: Session,
    *,
    retention_days: int,
    apply_changes: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> ResultadoLimpeza:
    """
    Aplica (ou so relata, se `apply_changes=False`) a retencao de
    `conversation_events`. Elegibilidade e SEMPRE `created_at < cutoff`
    (estrito) — uma linha exatamente NO cutoff permanece.

    `apply_changes=False` (dry run) nunca constroi nem executa um DELETE:
    so conta. `apply_changes=True` deleta em lotes de `batch_size` ids (id
    ascendente), cada lote commitado como transacao propria — nunca carrega
    todas as linhas elegiveis de uma vez.
    """
    cutoff = calcular_cutoff(retention_days, now=now)

    elegivel_total = (
        db.query(func.count(ConversationEvent.id))
        .filter(ConversationEvent.created_at < cutoff)
        .scalar()
        or 0
    )
    por_tipo = _contagem_por_tipo(db, cutoff)

    deletado_total = 0
    if apply_changes:
        while True:
            ids_lote = [
                id_
                for (id_,) in (
                    db.query(ConversationEvent.id)
                    .filter(ConversationEvent.created_at < cutoff)
                    .order_by(ConversationEvent.id)
                    .limit(batch_size)
                    .all()
                )
            ]
            if not ids_lote:
                break
            try:
                linhas_deletadas = (
                    db.query(ConversationEvent)
                    .filter(ConversationEvent.id.in_(ids_lote))
                    .delete(synchronize_session=False)
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            deletado_total += linhas_deletadas

    return ResultadoLimpeza(
        cutoff=cutoff,
        elegivel_total=elegivel_total,
        por_tipo=por_tipo,
        deletado_total=deletado_total,
        aplicado=apply_changes,
    )


def _relatorio(resultado: ResultadoLimpeza) -> None:
    """
    So identificadores tecnicos/agregados — NUNCA payload, whatsapp_msg_id
    ou qualquer outro campo de linha (dry run e apply seguem a MESMA regra).
    """
    modo = "APLICADO" if resultado.aplicado else "DRY RUN"
    print(f"[cleanup_conversation_events] modo: {modo}")
    print(f"[cleanup_conversation_events] cutoff (UTC): {resultado.cutoff.isoformat()}")
    print(f"[cleanup_conversation_events] elegiveis (created_at < cutoff): {resultado.elegivel_total}")
    for tipo, total in resultado.por_tipo:
        print(f"[cleanup_conversation_events]   event_type={tipo}: {total}")
    if resultado.aplicado:
        print(f"[cleanup_conversation_events] deletados: {resultado.deletado_total}")
    else:
        print(
            "[cleanup_conversation_events] nenhuma linha deletada (dry run) "
            "— use --apply --confirm para aplicar de verdade"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Limpeza/retencao de conversation_events (WP R13). Dry run por "
            "default — nunca deleta sem --apply --confirm."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Deleta de verdade as linhas elegiveis (exige --confirm junto).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Segunda confirmacao explicita — obrigatoria junto de --apply.",
    )
    args = parser.parse_args(argv)

    recusado = args.apply and not args.confirm
    if recusado:
        print(
            "[cleanup_conversation_events] RECUSADO — --apply sem --confirm nao "
            "deleta nada. Rode com --apply --confirm para aplicar de verdade."
        )
    apply_changes = args.apply and args.confirm

    db = SessionLocal()
    try:
        resultado = limpar_eventos_antigos(
            db,
            retention_days=CONVERSATION_EVENTS_RETENTION_DAYS,
            apply_changes=apply_changes,
        )
    except Exception:
        db.rollback()
        logger.exception("[cleanup_conversation_events] falha inesperada")
        print("[cleanup_conversation_events] FALHOU — ver log para detalhes (nenhum dado sensivel impresso)")
        return 1
    finally:
        db.close()

    _relatorio(resultado)
    return 1 if recusado else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main(sys.argv[1:]))
