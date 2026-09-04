"""
Operacao comercial idempotente do CRM — WP BIA-V2, Fase 6 (Handoff).

`POST /api/v2/lead-domain/handoff` e o lado CRM do handoff: o Conversas decide
que a conversa vai para um humano e chama este endpoint com um `event_id` como
chave de idempotencia. O CRM aplica `lead.responsavel_id = target_user_id`, uma
unica vez por `event_id`, e diz se aquilo foi aplicado agora ou se e repeticao.

PRECEDENTE DE DESENHO — E O QUE MUDA EM RELACAO A ELE
-----------------------------------------------------
A rota mais proxima que ja existe e `PUT /api/leads/{lead_id}/responsavel`
(`app/routers/leads.py`): autentica com `get_current_user` (nao `require_admin`),
valida que o usuario-alvo existe e esta ativo, grava `LeadHistory` na MESMA
transacao da troca e so depois dispara efeito externo. O espirito e o mesmo
aqui: a escrita local fecha primeiro e nunca fica refem de nada de fora.

Tres coisas mudam, e sao o motivo de existir um endpoint novo em vez de um
parametro na rota antiga:

  * Chamador servidor-a-servidor, autenticado por `X-API-Key` de um usuario
    real da tabela `users` compartilhada — nao uma sessao de navegador.
  * Idempotencia REAL por `event_id`, que a rota antiga nao tem: repetir o
    `PUT` reaplica.
  * Sem efeito colateral de saida. A rota antiga chama de volta o Conversas
    (`conversas_bridge.notificar_handoff`); aqui o Conversas E o chamador —
    fechar o ciclo produziria recursao entre os dois servicos.

AUTORIZACAO
-----------
`Depends(get_current_user)`, a mesma dependency do precedente, que ja aceita
`X-API-Key` (`app/auth.py`). Nao e `require_admin` de proposito: exigir admin
obrigaria a chave usada pelo Conversas a pertencer a um administrador, um
privilegio MAIOR do que a operacao precisa. Trocar o responsavel de um lead e
operacao de usuario comum em todo o resto do CRM — o precedente acima e
`get_current_user`, e alinhar com ele mantem o menor privilegio possivel para
a credencial de integracao.

Autenticar nao basta, e essa e a correcao de um achado de seguranca real desta
revisao: esta rota aplica `lead.responsavel_id` SEM passar pela ponte
`conversas_bridge.notificar_handoff` que `PUT /api/leads/{lead_id}/responsavel`
bloqueia deliberadamente por esse motivo exato (`app/routers/leads.py:632-649`,
o 422 "Use PUT .../responsavel" e o precedente citado ali). Se QUALQUER sessao
autenticada pudesse chamar este endpoint, ela reabriria exatamente o buraco que
aquele bloqueio fecha: lead mudando de dono sem a conversa do WhatsApp sair da
Bia e entrar na fila.

Por isso o handler tambem exige, via `_conferir_chamador_autorizado`, que
`current_user.id` seja o `V2_LEAD_DOMAIN_SERVICE_USER_ID` configurado
(`app/config.py`) — a MESMA identidade cuja API key vira `BIA_V2_CRM_API_KEY` do lado
Conversas. Qualquer outro `current_user` autenticado (inclusive um usuario
comum valido, inclusive um admin) recebe 403 — nunca 404, que esconderia a
existencia da rota de um jeito inconsistente com o resto da API. A checagem e
por id, nao por role: continua sendo `get_current_user`, nao `require_admin` —
a diferenca e que agora so UM principal especifico pode usar essa credencial,
em vez de qualquer usuario comum.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.lead import Lead
from app.models.lead_handoff_operation import LeadHandoffOperation
from app.models.pipeline import LeadHistory
from app.models.user import User
from app.schemas.lead_domain import HandoffRequest, HandoffResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/lead-domain", tags=["V2 — Dominio de Lead"])


class HandoffIndeterminado(Exception):
    """O banco recusou a gravacao por um motivo que nao e a colisao esperada.

    Unica excecao de dominio deste modulo que NAO e um `HTTPException`, e por
    isso a unica que precisa de handler registrado na instancia FastAPI raiz
    (`app/main.py`) — um handler em `APIRouter` nunca e chamado. Sem ele, este
    caminho viraria o 500 generico do Starlette, que e exatamente o que o plano
    proibe: nenhuma excecao de dominio pode virar 500 por acidente.

    O texto que chega ao cliente e generico DE PROPOSITO: o `IntegrityError`
    original carrega SQL, nome de constraint e nome de coluna, e nada disso
    pode sair na resposta. O detalhe vai para o log do servidor.
    """


def _conferir_chamador_autorizado(current_user: User) -> None:
    """403 se `current_user` nao for o usuario de servico configurado.

    Ver a secao AUTORIZACAO no topo do modulo para o motivo: sem esta checagem,
    qualquer sessao autenticada reabriria a troca de `responsavel_id` sem a
    ponte para o inbox que `PUT /api/leads/{lead_id}/responsavel` bloqueia de
    proposito. Nunca 404 aqui — 404 esconderia a existencia da rota de um jeito
    inconsistente com o resto da API, que devolve 403 para autenticado-mas-sem-
    permissao (ver `require_admin` em `app/auth.py`).

    Le `app.config` PELO MODULO, nao por `from app.config import ...`: o valor
    e lido em tempo de chamada, para que o teste possa sobrescrever
    `config.V2_LEAD_DOMAIN_SERVICE_USER_ID` em runtime depois de criar o
    usuario de teste e descobrir o id real — mesmo padrao de
    `_get_user_from_internal_ai` (`app/auth.py`).
    """
    from app import config

    if current_user.id != config.V2_LEAD_DOMAIN_SERVICE_USER_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito ao usuario de servico do handoff",
        )


def _carregar_lead(db: Session, lead_id: int) -> Lead:
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead nao encontrado")
    return lead


def _carregar_usuario_ativo(db: Session, target_user_id: int) -> User:
    """404 se nao existe, 422 se existe e esta inativo.

    A rota V1 funde os dois casos num 404 ("Usuario nao encontrado ou
    inativo"). Aqui eles sao separados porque o chamador e uma maquina que
    precisa saber o que fazer: id inexistente e erro de dado (nao adianta
    repetir), usuario inativo e condicao operacional (pode voltar a valer).
    A separacao nao expande informacao: quem chama ja esta autenticado e ja
    pode listar usuarios pela API.
    """
    usuario = db.query(User).filter(User.id == target_user_id).first()
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Usuario alvo nao encontrado"
        )
    if not usuario.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Usuario alvo esta inativo",
        )
    return usuario


def _buscar_registro(db: Session, event_id: str) -> LeadHandoffOperation | None:
    return (
        db.query(LeadHandoffOperation)
        .filter(LeadHandoffOperation.event_id == event_id)
        .first()
    )


def _conferir_parametros(registro: LeadHandoffOperation, dados: HandoffRequest) -> None:
    """409 quando o mesmo `event_id` volta com outros parametros.

    Retry inconsistente ou adulterado. Nao e idempotencia: reaplicar moveria o
    lead ERRADO, e devolver 200 afirmaria um resultado que nunca aconteceu.
    """
    if registro.lead_id != dados.lead_id or registro.target_user_id != dados.target_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="event_id ja processado com parametros diferentes",
        )


def _resolver_colisao(db: Session, dados: HandoffRequest) -> LeadHandoffOperation:
    """Decide o que a colisao de `event_id` no SAVEPOINT significa.

    Chamada SO depois de um `IntegrityError`. Reconsulta antes de tratar
    qualquer coisa como duplicata — mesmo padrao de `garantir_entrada_no_funil`
    e `_obter_ou_criar_tag` (`app/services/lead_creation.py`): no PostgreSQL o
    indice unico BLOQUEIA o segundo inserinte ate o primeiro commitar, entao o
    re-SELECT enxerga a linha que venceu a corrida.
    """
    registro = _buscar_registro(db, dados.event_id)
    if registro is None:
        # Nao era a violacao esperada. Engolir aqui devolveria 200 sobre uma
        # gravacao que NAO aconteceu — mesma armadilha que `lead_creation.py`
        # evita levantando o original em vez de retornar None.
        raise HandoffIndeterminado(
            f"IntegrityError sem registro correspondente para event_id={dados.event_id}"
        )
    _conferir_parametros(registro, dados)
    return registro


def _responder_replay(registro: LeadHandoffOperation) -> HandoffResponse:
    logger.info(
        "handoff replay: event_id=%s lead_id=%s target_user_id=%s (nenhum efeito aplicado)",
        registro.event_id, registro.lead_id, registro.target_user_id,
    )
    return HandoffResponse(
        lead_id=registro.lead_id,
        target_user_id=registro.target_user_id,
        event_id=registro.event_id,
        replayed=True,
    )


# Rota SINCRONA (`def`, nao `async def`) de proposito: a `Session` do
# SQLAlchemy usada aqui e sincrona e bloqueante, e o FastAPI ja roda rota
# sincrona em threadpool. Dentro de um `async def` ela bloquearia o event loop.
#
# Ordem das etapas do handler nao e arbitraria (depois da autorizacao, que
# vem antes de tudo — ver `_conferir_chamador_autorizado`):
#
#   1. Consultar o livro-razao PRIMEIRO. Um `event_id` ja aplicado responde
#      replay sem revalidar nada — ver "por que o replay vem antes" abaixo.
#   2. So entao validar lead e usuario. Um 404/422 aqui nao deixa nada
#      meio-aplicado e NAO consome o `event_id`: a mesma chave pode ser
#      reenviada quando a condicao se resolver.
#   3. Gravar o livro-razao dentro de um SAVEPOINT, ANTES de tocar o lead —
#      assim uma colisao (a corrida que a etapa 1 nao pegou) encontra a
#      transacao limpa, sem mutacao para desfazer.
#   4. So depois aplicar a mudanca no lead e o rastro em `lead_history`.
#   5. UM `db.commit()` no fim, dono explicito da transacao: livro-razao,
#      lead e historico entram juntos ou nao entram.
#
# POR QUE O REPLAY VEM ANTES DA VALIDACAO
# ----------------------------------------
# Validar primeiro parece mais seguro e nao e. Cenario real: o handoff para a
# Ana e aplicado, a resposta se perde no caminho, a Ana e desativada, e o
# Conversas reenvia o MESMO event_id. Com a validacao na frente, a resposta
# seria 422 "usuario inativo" — o CRM afirmando que a operacao falhou quando
# ela esta aplicada e o lead esta com a Ana. O Conversas marcaria
# HANDOFF_FAILED sobre um handoff bem-sucedido, e os dois sistemas
# divergiriam. O mesmo vale para 404 depois de o lead ser apagado (e por isso
# o livro-razao nao tem ForeignKey: ele sobrevive ao lead).
#
# Consultar o livro-razao primeiro nao enfraquece 404/422: um event_id INEDITO
# com alvo inexistente ou inativo continua sendo recusado, porque nao ha
# registro para encontrar. O que muda e so a resposta a uma repeticao, que
# passa a dizer a verdade sobre o que ja aconteceu — que e a definicao de
# idempotencia no contrato ("mesmo resultado, zero efeito duplicado").
#
# A etapa 1 nao dispensa o SAVEPOINT: entre a consulta e o INSERT cabe outra
# requisicao com o mesmo event_id. A etapa 1 e o caminho rapido do replay; o
# SAVEPOINT e quem fecha a corrida. Mesma divisao de `_obter_ou_criar_tag`.
#
# Sobre o R12 (o SAVEPOINT do pysqlite que pode comitar cedo em SQLite, e so em
# SQLite): entre o RELEASE do savepoint e o `db.commit()` so ha atribuicao em
# memoria e `db.add()` — nada que faca uma chamada de rede ou validacao capaz
# de falhar POR SI SO nessa janela. Isso e requisito de desenho, nao
# coincidencia: se algo falivel entrasse ai, em SQLite o livro-razao poderia
# ficar gravado sem o efeito no lead, e o replay seguinte devolveria
# replayed=true para um handoff que nunca aconteceu. Nao acrescente passo
# falivel nesta janela.
#
# Essa garantia cobre SO o que acontece ANTES do `db.commit()`. O proprio
# commit falhar e outra historia, e a atomicidade nesse caso so esta provada
# no PostgreSQL (producao): la, um commit que falha desfaz TUDO, savepoint ja
# liberado incluso. Em SQLite (dev), o comportamento de um commit que falha
# DEPOIS de um SAVEPOINT liberado NAO foi verificado — a lacuna conhecida do
# driver pysqlite (`.claude/memory/pysqlite-savepoint-rollback-gap.md`) so
# testa um `db.rollback()` explicito, nao uma falha dentro do proprio
# `commit()`. Nao trate esta garantia como universal.
@router.post(
    "/handoff",
    response_model=HandoffResponse,
    summary="Aplicar handoff de lead para um humano (idempotente por event_id)",
)
def aplicar_handoff(
    dados: HandoffRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HandoffResponse:
    """Aplica `lead.responsavel_id = target_user_id` uma unica vez por `event_id`.

    Idempotente por `event_id`: repetir a MESMA chave com os mesmos `lead_id` e
    `target_user_id` devolve o mesmo resultado (`replayed=true`), sem aplicar o
    efeito uma segunda vez. O mesmo `event_id` com parametros diferentes e
    recusado (409). `lead_id` ou `target_user_id` inexistentes retornam 404;
    `target_user_id` inativo retorna 422 — nestes dois casos o `event_id` NAO e
    consumido e pode ser reenviado quando a condicao se resolver.
    """
    _conferir_chamador_autorizado(current_user)

    ja_aplicado = _buscar_registro(db, dados.event_id)
    if ja_aplicado is not None:
        _conferir_parametros(ja_aplicado, dados)
        return _responder_replay(ja_aplicado)

    lead = _carregar_lead(db, dados.lead_id)
    _carregar_usuario_ativo(db, dados.target_user_id)

    operacao = LeadHandoffOperation(
        event_id=dados.event_id,
        lead_id=dados.lead_id,
        target_user_id=dados.target_user_id,
    )
    try:
        # SAVEPOINT, nunca um `db.rollback()` direto: um `IntegrityError` aqui
        # significa que outra requisicao com o MESMO event_id venceu a corrida,
        # nao que a transacao inteira deva ser descartada. O SAVEPOINT desfaz
        # so o INSERT que colidiu e deixa a sessao utilizavel para a
        # reconsulta logo abaixo.
        with db.begin_nested():
            db.add(operacao)
            db.flush()
    except IntegrityError:
        return _responder_replay(_resolver_colisao(db, dados))
    except SQLAlchemyError as exc:
        # Qualquer OUTRA falha do SQLAlchemy dentro do SAVEPOINT (ex.:
        # OperationalError por timeout de lock, conexao derrubada) NAO e a
        # colisao esperada do UNIQUE, e por isso nao pode virar replay: o
        # estado da gravacao ficou desconhecido. Mesmo tratamento que
        # `_resolver_colisao` da ao "IntegrityError sem registro
        # correspondente" — levanta `HandoffIndeterminado`, que tem handler
        # dedicado (`app/main.py`) com log estruturado e 500 de corpo
        # generico, nunca o 500 cru do Starlette.
        raise HandoffIndeterminado(
            f"{type(exc).__name__} nao esperado no SAVEPOINT do handoff "
            f"event_id={dados.event_id}: {exc}"
        ) from exc

    responsavel_anterior = lead.responsavel_id
    if responsavel_anterior != dados.target_user_id:
        lead.responsavel_id = dados.target_user_id
        # Mesmo evento e mesma forma de `dados` que a rota V1 grava, para que a
        # timeline do lead nao tenha dois formatos para a mesma mudanca. So
        # quando o valor muda de verdade — igual a V1, e por isso o livro-razao
        # e uma tabela separada (ver o docstring do model).
        db.add(LeadHistory(
            lead_id=lead.id,
            evento="responsavel_changed",
            descricao=(
                f"Responsavel alterado de "
                f"'{'Agente IA' if responsavel_anterior is None else responsavel_anterior}' "
                f"para '{dados.target_user_id}' (handoff V2)"
            ),
            dados={
                "old_responsavel_id": responsavel_anterior,
                "new_responsavel_id": dados.target_user_id,
                "event_id": dados.event_id,
                "origem": "v2_lead_domain.handoff",
            },
        ))

    db.commit()

    logger.info(
        "handoff aplicado: event_id=%s lead_id=%s responsavel %s -> %s (por user_id=%s)",
        dados.event_id, dados.lead_id, responsavel_anterior, dados.target_user_id,
        current_user.id,
    )
    return HandoffResponse(
        lead_id=dados.lead_id,
        target_user_id=dados.target_user_id,
        event_id=dados.event_id,
        replayed=False,
    )
