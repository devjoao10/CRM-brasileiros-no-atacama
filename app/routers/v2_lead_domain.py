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
`get_current_user`, a mesma dependency do precedente (alcancada pelo wrapper
`get_servico_handoff_user`, abaixo), que ja aceita `X-API-Key`
(`app/auth.py`). Nao e `require_admin` de proposito: exigir admin
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

Por isso a rota depende de `get_servico_handoff_user`, que exige que
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
from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import IS_SQLITE, get_db
from app.models.lead import Lead
from app.models.lead_handoff_operation import LeadHandoffOperation
from app.models.pipeline import LeadHistory
from app.models.user import User
from app.schemas.lead_domain import HandoffRequest, HandoffResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/lead-domain", tags=["V2 — Dominio de Lead"])

# Espera maxima por um lock de linha DENTRO desta transacao. Precisa ser MENOR
# que o timeout de leitura do cliente Conversas (`httpx.Timeout(10.0, ...)`):
# sem ele, uma transacao que perde a corrida pelo lock pode ficar viva no
# servidor bem depois de o cliente ter desistido com `ReadTimeout` (rota
# sincrona em threadpool, sem cancelamento) e aplicar o handoff quando o
# Conversas ja classificou a chamada como ambigua. Ver `_definir_lock_timeout`.
_LOCK_TIMEOUT = "3s"


class TokenErroHandoff(StrEnum):
    """Vocabulario fechado de erro que este endpoint EMITE, em corpo estruturado.

    Todo nao-2xx desta rota responde `{"detail": {"error": "<TOKEN>"}}` — nunca
    prosa solta. O chamador e uma maquina: ela ramifica por token, e uma string
    livre obrigaria o Conversas a fazer parsing de frase, que quebra na primeira
    correcao de portugues.

    StrEnum LOCAL, dentro deste modulo, e nao um `constants.py` compartilhado:
    e o primeiro vocabulario desse tipo no CRM e segue o precedente do modulo
    irmao do lado Conversas (`conversas/app/v2/pipeline.py`, que define
    `EtapaFinal`/`ResultadoPipeline` no proprio arquivo). Sendo `StrEnum`, cada
    membro E uma `str` — entra direto em `detail=` e o `json.dumps` do Starlette
    serializa como a string do valor, sem `.value` explicito.

    DOIS MEMBROS NAO SAO EMITIDOS POR ESTE ARQUIVO, e isso e deliberado:
      * `CREDENCIAL_INVALIDA` (401) nasce em `get_current_user` (`app/auth.py`),
        dependency COMPARTILHADA por toda a API do CRM.
      * `CRM_RECUSADO_CORPO` (422 de schema) nasce no handler de
        `RequestValidationError` do proprio FastAPI, tambem global.
    Convertê-los aqui e impossivel; convertê-los na origem mudaria o corpo de
    erro de TODAS as rotas do CRM, muito alem do escopo deste endpoint. Ficam
    declarados porque sao parte do vocabulario que o Conversas mapeia por
    status code, e nomeá-los num so lugar evita que a proxima pessoa invente um
    terceiro nome para a mesma coisa.
    """

    CREDENCIAL_INVALIDA = "CREDENCIAL_INVALIDA"
    CHAMADOR_NAO_AUTORIZADO = "CHAMADOR_NAO_AUTORIZADO"
    LEAD_NOT_FOUND = "LEAD_NOT_FOUND"
    TARGET_USER_NOT_FOUND = "TARGET_USER_NOT_FOUND"
    TARGET_USER_INACTIVE = "TARGET_USER_INACTIVE"
    CRM_RECUSADO_CORPO = "CRM_RECUSADO_CORPO"
    HANDOFF_NAO_APLICADO = "HANDOFF_NAO_APLICADO"
    IDEMPOTENCY_PAYLOAD_CONFLICT = "IDEMPOTENCY_PAYLOAD_CONFLICT"
    LEAD_ALREADY_ASSIGNED = "LEAD_ALREADY_ASSIGNED"


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


def get_servico_handoff_user(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: 403 se `current_user` nao for o usuario de servico configurado.

    Ver a secao AUTORIZACAO no topo do modulo para o motivo: sem esta checagem,
    qualquer sessao autenticada reabriria a troca de `responsavel_id` sem a
    ponte para o inbox que `PUT /api/leads/{lead_id}/responsavel` bloqueia de
    proposito. Nunca 404 aqui — 404 esconderia a existencia da rota de um jeito
    inconsistente com o resto da API, que devolve 403 para autenticado-mas-sem-
    permissao (ver `require_admin` em `app/auth.py`).

    E uma DEPENDENCY, nao uma chamada dentro do handler, pelo mesmo motivo que
    `require_admin` (`app/auth.py`) e uma: autorizacao desta API se declara na
    assinatura da rota, onde quem le o endpoint enxerga o guard sem entrar no
    corpo — e o OpenAPI passa a listar `get_current_user` como dependencia
    resolvida antes de qualquer validacao de corpo.

    Le `app.config` PELO MODULO, nao por `from app.config import ...`: o valor
    e lido em tempo de chamada, para que o teste possa sobrescrever
    `config.V2_LEAD_DOMAIN_SERVICE_USER_ID` em runtime depois de criar o
    usuario de teste e descobrir o id real — mesmo padrao de
    `_get_user_from_internal_ai` (`app/auth.py`). Vale tambem aqui dentro de um
    `Depends`: o corpo da funcao so roda por requisicao, nunca no import.
    """
    from app import config

    if current_user.id != config.V2_LEAD_DOMAIN_SERVICE_USER_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": TokenErroHandoff.CHAMADOR_NAO_AUTORIZADO},
        )
    return current_user


def _definir_lock_timeout(db: Session) -> None:
    """`SET LOCAL lock_timeout` para esta transacao — nunca um `SET` puro.

    `LOCAL` limita o efeito a transacao corrente. Um `SET` sem `LOCAL` grudaria
    na CONEXAO, e a conexao volta para o pool (`app/database.py`, `pool_size=10`
    + `max_overflow=20`): o timeout vazaria para a proxima requisicao que
    reusasse essa conexao, uma rota qualquer do CRM que nunca pediu isso.

    No-op em SQLite (dev/CI): `SET` nao existe la, e o dialeto tambem nao tem
    espera por lock de linha para limitar.

    O valor entra por f-string, e nao por bind parameter, porque `SET LOCAL` do
    PostgreSQL nao aceita placeholder — e `_LOCK_TIMEOUT` e um literal do modulo,
    nunca entrada de usuario. Nao troque a constante por algo vindo da request.
    """
    if IS_SQLITE:
        return
    db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _carregar_lead_sob_lock(db: Session, lead_id: int) -> Lead:
    """Le o lead SOB LOCK DE ESCRITA e produz o 404 — as duas coisas na MESMA query.

    O nome carrega "sob lock" de proposito: esta funcao so pode ser usada no
    caminho que VAI ESCREVER. O ramo de replay le o lead sem lock, inline, e
    nao pode chamar isto (levantaria 404 sobre uma operacao ja aplicada).

    NAO separe isto em "carrega e depois tranca". A leitura pre-lock deixaria o
    `Lead` no identity map do SQLAlchemy, e um `.with_for_update().first()`
    posterior adquiriria o lock de linha DE VERDADE no PostgreSQL mas devolveria
    o MESMO objeto Python ja em memoria, com `responsavel_id` OBSOLETO — o
    compare-and-swap compararia contra lixo e passaria quando nao devia. Nao e
    hipotese: com essa forma, 39/40 rodadas com 2 threads e 8/8 com 3 processos
    reais aplicaram os dois handoffs concorrentes (`bia-v2-rb-proof.md`).

    `.populate_existing()` e o que fecha isso: forca a re-hidratacao do objeto
    com os valores frescos lidos sob o lock. A alternativa equivalente seria
    `db.refresh(lead, with_for_update=True)` (idioma que
    `conversas/app/v2/handoff.py:_aplicar_estado_local` ja usa). A forma SEM um
    dos dois NAO e aceitavel — e ela parece funcionar em qualquer teste nao
    concorrente, que e exatamente o que torna o erro facil de reintroduzir.
    """
    lead = (
        db.query(Lead)
        .filter(Lead.id == lead_id)
        .with_for_update()
        .populate_existing()  # obrigatorio — sem isto, o identity map vence
        .first()
    )
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": TokenErroHandoff.LEAD_NOT_FOUND},
        )
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
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": TokenErroHandoff.TARGET_USER_NOT_FOUND},
        )
    if not usuario.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": TokenErroHandoff.TARGET_USER_INACTIVE},
        )
    return usuario


def _buscar_registro(db: Session, event_id: str) -> LeadHandoffOperation | None:
    return (
        db.query(LeadHandoffOperation)
        .filter(LeadHandoffOperation.event_id == event_id)
        .first()
    )


def _buscar_dono_atual(
    db: Session, lead_id: int, responsavel_id: int | None
) -> LeadHandoffOperation | None:
    """Busca REVERSA, POR VALOR, da operacao que provavelmente pos o dono atual.

    RESSALVA DE CONFIABILIDADE, deliberadamente documentada aqui e nao escondida
    na implementacao: `lead_handoff_operations` e append-only e NAO tem coluna
    apontando "esta e a operacao que produziu o valor atual". A busca e por
    `(lead_id, target_user_id = responsavel atual)`, entao se o MESMO usuario ja
    foi responsavel por este lead mais de uma vez ao longo do tempo — ou se o
    dono atual foi posto por um caminho que nao grava no livro-razao, como
    `PUT /api/leads/{id}/responsavel` (V1) — o resultado pode ser uma operacao
    V2 antiga, ou nenhuma.

    Isso e ACEITAVEL porque os dois campos derivados daqui
    (`current_owner_event_id`/`current_owner_applied_at`) sao estritamente
    diagnosticos: o Conversas nunca ramifica decisao sobre eles, so sobre
    `detail.error`. Nao construa nada que dependa da exatidao desta busca.
    """
    if responsavel_id is None:
        # `target_user_id` e NOT NULL — nao existe linha para "ninguem".
        return None
    return (
        db.query(LeadHandoffOperation)
        .filter(
            LeadHandoffOperation.lead_id == lead_id,
            LeadHandoffOperation.target_user_id == responsavel_id,
        )
        .order_by(LeadHandoffOperation.created_at.desc(), LeadHandoffOperation.id.desc())
        .first()
    )


def _erro_lead_ja_atribuido(
    db: Session, dados: HandoffRequest, current_responsavel_id: int | None
) -> HTTPException:
    """Monta o 409 `LEAD_ALREADY_ASSIGNED`. Devolve — quem chama e que levanta.

    DOIS EMISSORES, mesma resposta de proposito:
      (a) primeira aplicacao que PERDEU o compare-and-swap para OUTRO
          `event_id` — a escrita deste chamador genuinamente nao aconteceu. Ler
          "outro" ai e essencial: perder o CAS para a gemea concorrente da
          MESMA chave e replay, nao conflito, e o ramo de primeira aplicacao
          reconsulta o livro-razao antes de chegar aqui;
      (b) replay cujo `event_id` ja aplicou no passado, mas o dono do lead MUDOU
          desde entao — aqui a escrita aconteceu, so foi superada por algo mais
          recente.
    O comportamento resultante e identico (nao aplicar localmente, parar de
    tentar), e por isso os dois usam o MESMO token. `expected_responsavel_id` no
    corpo so tem significado no caso (a) — no (b) o CAS nunca chegou a ser
    avaliado; fica ecoado por uniformidade de schema, nao por carregar
    diagnostico.

    `current_responsavel_id` pode ser `None` de forma legitima: entre um release
    e a reatribuicao seguinte o lead fica sem dono, e `registro.target_user_id`
    nunca e nulo, entao a comparacao do replay sempre acusa "diferente" nessa
    janela. O nome do token engana a primeira leitura ("ja atribuido" com valor
    `None`), mas o que ele comunica e "o valor que voce esperava nao e mais
    verdade; o valor real esta no corpo" — inclusive quando o valor real e
    ninguem. Quem consome precisa tratar `None` como valor, nao como ausencia.
    """
    dono = _buscar_dono_atual(db, dados.lead_id, current_responsavel_id)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": TokenErroHandoff.LEAD_ALREADY_ASSIGNED,
            "current_responsavel_id": current_responsavel_id,
            "expected_responsavel_id": dados.expected_responsavel_id,
            "current_owner_event_id": dono.event_id if dono is not None else None,
            # `.isoformat()` EXPLICITO: `created_at` e `DateTime(timezone=True)`,
            # hidratado como objeto `datetime` vivo. O `json.dumps` do
            # `JSONResponse` do Starlette nao serializa `datetime` — estouraria
            # `TypeError` DENTRO do handler de excecao, que ninguem recaptura,
            # degradando para um 500 generico e destruindo o corpo estruturado
            # exatamente no caso em que ele mais importa.
            "current_owner_applied_at": (
                dono.created_at.isoformat()
                if dono is not None and dono.created_at is not None
                else None
            ),
        },
    )


def _conferir_parametros(
    registro: LeadHandoffOperation,
    dados: HandoffRequest,
    current_responsavel_id: int | None,
) -> None:
    """409 quando o mesmo `event_id` volta com outros parametros.

    Retry inconsistente ou adulterado. Nao e idempotencia: reaplicar moveria o
    lead ERRADO, e devolver 200 afirmaria um resultado que nunca aconteceu.

    O corpo carrega `registered_lead_id`/`registered_target_user_id` — os valores
    GRAVADOS no livro-razao, nao os recebidos no payload. E essa a pergunta que
    o chamador precisa responder aqui ("com que parametros este `event_id` foi
    de fato aplicado?"); o payload que ele acabou de mandar ele ja tem. Nao ha
    divulgacao nova: e o registro DELE, sob o `event_id` que ELE reenviou.
    `current_responsavel_id` (o dono ATUAL do lead) responde outra pergunta e
    vem junto, sempre presente nos dois casos de 409 desta rota.
    """
    if registro.lead_id != dados.lead_id or registro.target_user_id != dados.target_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": TokenErroHandoff.IDEMPOTENCY_PAYLOAD_CONFLICT,
                "current_responsavel_id": current_responsavel_id,
                "registered_lead_id": registro.lead_id,
                "registered_target_user_id": registro.target_user_id,
            },
        )


def _resolver_colisao(
    db: Session, dados: HandoffRequest, current_responsavel_id: int | None
) -> LeadHandoffOperation:
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
    _conferir_parametros(registro, dados, current_responsavel_id=current_responsavel_id)
    return registro


def _responder_replay(
    registro: LeadHandoffOperation, current_responsavel_id: int | None
) -> HandoffResponse:
    logger.info(
        "handoff replay: event_id=%s lead_id=%s target_user_id=%s "
        "responsavel_atual=%s (nenhum efeito aplicado)",
        registro.event_id, registro.lead_id, registro.target_user_id,
        current_responsavel_id,
    )
    return HandoffResponse(
        lead_id=registro.lead_id,
        target_user_id=registro.target_user_id,
        event_id=registro.event_id,
        replayed=True,
        current_responsavel_id=current_responsavel_id,
    )


# Contrato de erro no OpenAPI/Swagger. Sem isto a rota documenta so o 200 e o
# 422 automatico do FastAPI, e o vocabulario fechado que o Conversas ramifica —
# `TokenErroHandoff` — fica invisivel para quem le a documentacao em vez do
# codigo, que e exatamente quem mais precisa dele.
#
# Os tokens sao INTERPOLADOS do proprio enum, nunca redigitados: `StrEnum`
# formata como o valor, entao renomear um membro atualiza a documentacao junto
# em vez de deixar duas verdades divergindo em silencio. Nao ha `BaseModel` por
# status de proposito — o corpo de erro tem forma diferente por token, e um
# schema por status seria mais ruido de manutencao do que informacao.
_RESPOSTAS_DE_ERRO = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": (
            f"Sem credencial ou credencial invalida "
            f"(`{TokenErroHandoff.CREDENCIAL_INVALIDA}` no vocabulario do "
            f"chamador). Nasce em `get_current_user`, dependency comum a toda a "
            f"API do CRM: o corpo e o `detail` de texto padrao dela, NAO o "
            f"objeto estruturado desta rota."
        )
    },
    status.HTTP_403_FORBIDDEN: {
        "description": (
            f"Autenticado, mas o chamador nao e o usuario de servico "
            f"configurado: `detail.error = "
            f"{TokenErroHandoff.CHAMADOR_NAO_AUTORIZADO}`."
        )
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            f"`detail.error = {TokenErroHandoff.LEAD_NOT_FOUND}` (lead_id "
            f"inexistente) ou `{TokenErroHandoff.TARGET_USER_NOT_FOUND}` "
            f"(target_user_id inexistente). O `event_id` NAO e consumido e pode "
            f"ser reenviado. Inalcancavel pelo caminho de replay."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            f"`detail.error = {TokenErroHandoff.LEAD_ALREADY_ASSIGNED}` — "
            f"compare-and-swap perdido para outra operacao, ou replay cujo dono "
            f"do lead ja mudou; o corpo traz `current_responsavel_id`, "
            f"`expected_responsavel_id`, `current_owner_event_id` e "
            f"`current_owner_applied_at`. Ou `detail.error = "
            f"{TokenErroHandoff.IDEMPOTENCY_PAYLOAD_CONFLICT}` — o mesmo "
            f"`event_id` voltou com outros parametros; o corpo traz "
            f"`registered_lead_id` e `registered_target_user_id`, os valores "
            f"GRAVADOS no livro-razao."
        )
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: {
        "description": (
            f"`detail.error = {TokenErroHandoff.TARGET_USER_INACTIVE}` — o "
            f"usuario alvo existe mas esta inativo, e o `event_id` NAO e "
            f"consumido. Tambem cobre o 422 de schema do proprio FastAPI "
            f"(`{TokenErroHandoff.CRM_RECUSADO_CORPO}` para o chamador), cujo "
            f"corpo e o `ValidationError` padrao, NAO o objeto estruturado "
            f"desta rota."
        )
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "description": (
            f"`detail.error = {TokenErroHandoff.HANDOFF_NAO_APLICADO}` — falha "
            f"de banco que deixa o estado da gravacao DESCONHECIDO "
            f"(`HandoffIndeterminado`, handler em `app/main.py`). Corpo "
            f"generico de proposito: o detalhe vai para o log do servidor."
        )
    },
}


# Rota SINCRONA (`def`, nao `async def`) de proposito: a `Session` do
# SQLAlchemy usada aqui e sincrona e bloqueante, e o FastAPI ja roda rota
# sincrona em threadpool. Dentro de um `async def` ela bloquearia o event loop.
#
# Ordem das etapas do handler nao e arbitraria (depois da autorizacao, que a
# dependency `get_servico_handoff_user` resolve antes de o corpo rodar):
#
#   1. Consultar o livro-razao PRIMEIRO, SEM lock. Um `event_id` ja aplicado
#      responde replay sem revalidar nada — ver "por que o replay vem antes"
#      abaixo. Esta consulta nao depende de o lead existir ou ser valido, e por
#      isso pode (e deve) vir na frente de tudo.
#   2a. RAMO DE REPLAY (o registro existe): leitura FRESCA do lead, SEM lock de
#      escrita — este ramo nao escreve nada, e um lock de escrita aqui so
#      serviria para dar uma falsa sensacao de fechamento (ver "por que o
#      replay le sem lock" abaixo). A leitura vem ANTES de `_conferir_parametros`
#      porque o 409 dele promete `current_responsavel_id` no corpo. Depois:
#      lead sumido -> devolve o resultado historico; dono bate com o registro ->
#      `replayed=true`; dono MUDOU -> 409 LEAD_ALREADY_ASSIGNED. Nunca 404/422.
#   2b. RAMO DE PRIMEIRA APLICACAO (nao ha registro): a query com lock E a que
#      produz o 404 do lead (`_carregar_lead_sob_lock`) — nunca uma leitura
#      solta antes dela. So entao valida o usuario (404/422). Um 404/422 aqui
#      nao deixa nada meio-aplicado e NAO consome o `event_id`: a mesma chave
#      pode ser reenviada quando a condicao se resolver.
#   3. Compare-and-swap contra `expected_responsavel_id`, ja com o valor lido
#      SOB o lock — e o que impede duas operacoes concorrentes de aplicarem as
#      duas. Perder o CAS nao e conflito por si so: antes de responder 409, o
#      livro-razao e RECONSULTADO por `event_id`, porque a operacao que ganhou
#      o lock pode ser a gemea concorrente da MESMA chave (ver o comentario no
#      proprio ramo).
#   4. Gravar o livro-razao dentro de um SAVEPOINT, ANTES de tocar o lead —
#      assim uma colisao (a corrida que a etapa 1 nao pegou) encontra a
#      transacao limpa, sem mutacao para desfazer.
#   5. So depois aplicar a mudanca no lead e o rastro em `lead_history`.
#   6. UM `db.commit()` no fim, dono explicito da transacao: livro-razao,
#      lead e historico entram juntos ou nao entram.
#
# Os DOIS ramos inteiros, do inicio ao fim, vivem dentro de um unico
# `try/except SQLAlchemyError -> HandoffIndeterminado`: a leitura sem lock do
# replay e uma etapa de banco como qualquer outra e merece o mesmo corpo
# estruturado de erro que o INSERT.
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
# POR QUE O REPLAY NAO PODE DEVOLVER O RESULTADO HISTORICO CEGAMENTE
# -------------------------------------------------------------------
# Dizer "ja apliquei, era o Fulano" e verdade sobre o PASSADO e pode ser mentira
# sobre o PRESENTE. O worker de retomada (R16) existe justamente para reenviar
# `event_id` antigos, e entre a aplicacao original e o reenvio cabe um release
# legitimo ou uma operacao mais recente que moveu o lead. Um replay cego faria o
# Conversas aplicar localmente um `target_user_id` que o CRM ja nao tem mais —
# os dois lados relatando sucesso enquanto divergem em silencio. Por isso o ramo
# de replay compara o dono FRESCO do lead contra `registro.target_user_id` e
# recusa com 409 quando nao bate.
#
# POR QUE O REPLAY LE SEM LOCK (e por que isso nao e descuido)
# ------------------------------------------------------------
# O replay e leitura pura, nao seguida de escrita — o lock de escrita pertence a
# operacao que vai escrever. O lock traria um beneficio parcial (esperaria um
# escritor concorrente ja em voo), mas NAO fecha o TOCTOU: um escritor que
# comece DEPOIS que esta transacao terminou produz a mesma divergencia com ou
# sem lock — nenhum lock de linha segura atraves de uma resposta HTTP ja
# enviada. A garantia forte e no cenario SEQUENCIAL (release ja commitado antes
# do replay comecar); sob concorrencia genuina o residuo permanece e e conhecido.
# Essa leitura sem lock depende de READ COMMITTED — invariante agora explicita
# em `app/database.py`, nao um default herdado por acidente.
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
    responses=_RESPOSTAS_DE_ERRO,
)
def aplicar_handoff(
    dados: HandoffRequest,
    current_user: User = Depends(get_servico_handoff_user),
    db: Session = Depends(get_db),
) -> HandoffResponse:
    """Aplica `lead.responsavel_id = target_user_id` uma unica vez por `event_id`.

    Idempotente por `event_id`: repetir a MESMA chave com os mesmos `lead_id` e
    `target_user_id` devolve o mesmo resultado (`replayed=true`), sem aplicar o
    efeito uma segunda vez — desde que o lead ainda esteja com o responsavel que
    aquele `event_id` aplicou; se o dono mudou depois, o replay recusa com 409
    `LEAD_ALREADY_ASSIGNED` em vez de afirmar um resultado ja superado. O mesmo
    `event_id` com parametros diferentes e recusado com 409
    `IDEMPOTENCY_PAYLOAD_CONFLICT`. `lead_id` ou `target_user_id` inexistentes
    retornam 404; `target_user_id` inativo retorna 422 — nestes dois casos o
    `event_id` NAO e consumido e pode ser reenviado quando a condicao se
    resolver, e nenhum dos dois e alcancavel pelo caminho de replay.

    `expected_responsavel_id` (opcional) e o compare-and-swap: quando presente,
    a aplicacao so acontece se `leads.responsavel_id` for exatamente esse valor
    no instante da leitura sob lock. Perder o CAS para a gemea concorrente da
    MESMA chave continua sendo replay (200), nao 409: o livro-razao e
    reconsultado por `event_id` antes de qualquer conflito ser declarado.
    """
    try:
        _definir_lock_timeout(db)
        registro = _buscar_registro(db, dados.event_id)

        if registro is not None:
            # RAMO DE REPLAY. Todo caminho daqui termina em `return` ou `raise`
            # — nenhum cai para a secao de primeira aplicacao abaixo. Trocar
            # isto por um `if` com fall-through reabre o bug de reaplicar um
            # `event_id` ja consumido.
            #
            # NUNCA revalida usuario nem levanta 404/422 neste ramo: um handoff
            # pode ter aplicado e SO DEPOIS o alvo ser desativado ou o lead
            # apagado, e o CRM nao pode dizer "falhou" sobre uma operacao que
            # esta aplicada. Ver "POR QUE O REPLAY VEM ANTES DA VALIDACAO".
            lead = (
                db.query(Lead)
                .filter(Lead.id == dados.lead_id)
                # leitura fresca; SEM with_for_update() — este ramo nao escreve
                .populate_existing()
                .first()
            )
            responsavel_atual = lead.responsavel_id if lead is not None else None

            # Precisa vir DEPOIS da leitura do lead: o 409 deste conferidor
            # promete `current_responsavel_id` no corpo, e sem o lead lido o
            # campo sairia vazio justamente aqui.
            _conferir_parametros(registro, dados, current_responsavel_id=responsavel_atual)

            if lead is None:
                # Lead apagado desde que o evento aplicou — nao ha dado com que
                # comparar. Devolve o resultado historico, que e a garantia que
                # o livro-razao sem ForeignKey existe para dar.
                return _responder_replay(registro, current_responsavel_id=None)
            if responsavel_atual == registro.target_user_id:
                return _responder_replay(registro, current_responsavel_id=responsavel_atual)
            # O dono MUDOU desde entao — inclusive para `None`, se o lead so foi
            # liberado e ainda nao reatribuido. Replayar cegamente mandaria o
            # Conversas aplicar um alvo ja superado.
            raise _erro_lead_ja_atribuido(db, dados, responsavel_atual)

        # PRIMEIRA APLICACAO. Aqui sim o lead precisa existir — e a query com
        # lock e, ela mesma, a que produz o 404 (ver `_carregar_lead_sob_lock`).
        lead = _carregar_lead_sob_lock(db, dados.lead_id)
        _carregar_usuario_ativo(db, dados.target_user_id)

        responsavel_anterior = lead.responsavel_id
        if "expected_responsavel_id" not in dados.model_fields_set:
            # Chamador antigo, anterior a esta versao — nao sabe que este campo
            # existe. Comportamento historico preservado: aplica sem checar.
            # `model_fields_set` e a unica forma de distinguir isto de um `null`
            # explicito; `.get()`/default funde os dois casos e faria todo
            # handoff para um lead que JA TEM dono (o cenario "cliente que
            # volta") falhar espuriamente enquanto o Conversas nao subisse.
            cas_ok = True
        else:
            # Chamador novo — `expected_responsavel_id` explicito, inclusive
            # quando e `None` (que aqui SIGNIFICA "espero o lead sem dono").
            cas_ok = responsavel_anterior == dados.expected_responsavel_id
        if not cas_ok:
            # RECONSULTA ANTES DE DECLARAR CONFLITO — o mesmo idioma que
            # `_resolver_colisao` ja usa depois do `IntegrityError`, agora no
            # ramo do compare-and-swap.
            #
            # Duas requisicoes com o MESMO `event_id` (duas instancias do worker
            # de retomada R16 podem legitimamente emitir as duas em paralelo)
            # veem o livro-razao vazio na etapa 1 e so entao se serializam no
            # `FOR UPDATE`. A perdedora rele o lead JA fresco, o CAS falha
            # contra o baseline antigo, e um 409 direto aqui seria regressao de
            # IDEMPOTENCIA: a MESMA chave receberia `LEAD_ALREADY_ASSIGNED` em
            # vez do replay que ela merece, e o Conversas classificaria a
            # propria repeticao como conflito de terceiro. A gemea ja commitou
            # (foi assim que o lock saiu), entao consultar por `event_id` agora,
            # sob o lock que ja temos, distingue os dois casos: sem registro =
            # perdi o CAS para OUTRA operacao (409); registro com os MESMOS
            # parametros = sou a repeticao da MINHA propria operacao (replay).
            #
            # NAO da para chamar `_resolver_colisao` aqui: sem registro ele
            # levanta `HandoffIndeterminado`, que e a leitura certa depois de um
            # `IntegrityError` (o banco recusou algo que ninguem explica) e a
            # errada aqui, onde "sem registro" e o conflito de CAS legitimo.
            gemea = _buscar_registro(db, dados.event_id)
            if gemea is None:
                raise _erro_lead_ja_atribuido(db, dados, responsavel_anterior)
            _conferir_parametros(gemea, dados, current_responsavel_id=responsavel_anterior)
            return _responder_replay(gemea, current_responsavel_id=responsavel_anterior)

        operacao = LeadHandoffOperation(
            event_id=dados.event_id,
            lead_id=dados.lead_id,
            target_user_id=dados.target_user_id,
            # Snapshot de "de quem" o lead saiu, lido SOB O LOCK e ANTES do
            # UPDATE abaixo — depois da atribuicao esse valor nao existe mais em
            # lugar nenhum.
            previous_responsavel_id=responsavel_anterior,
        )
        try:
            # SAVEPOINT, nunca um `db.rollback()` direto: um `IntegrityError`
            # aqui significa que outra requisicao com o MESMO event_id venceu a
            # corrida, nao que a transacao inteira deva ser descartada. O
            # SAVEPOINT desfaz so o INSERT que colidiu e deixa a sessao
            # utilizavel para a reconsulta logo abaixo. Qualquer OUTRA falha do
            # SQLAlchemy aqui cai no `except` externo desta funcao e vira
            # `HandoffIndeterminado` — nao pode virar replay, porque o estado da
            # gravacao ficou desconhecido.
            with db.begin_nested():
                db.add(operacao)
                db.flush()
        except IntegrityError:
            return _responder_replay(
                _resolver_colisao(db, dados, current_responsavel_id=responsavel_anterior),
                current_responsavel_id=responsavel_anterior,
            )

        if responsavel_anterior != dados.target_user_id:
            lead.responsavel_id = dados.target_user_id
            # Mesmo evento e mesma forma de `dados` que a rota V1 grava, para
            # que a timeline do lead nao tenha dois formatos para a mesma
            # mudanca. So quando o valor muda de verdade — igual a V1, e por
            # isso o livro-razao e uma tabela separada (ver o docstring do
            # model).
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
    except SQLAlchemyError as exc:
        # Envolve os DOIS ramos inteiros, do inicio ao fim: qualquer falha do
        # SQLAlchemy (OperationalError por `lock_timeout` estourado, conexao
        # derrubada, o proprio commit falhando) deixa o estado da gravacao
        # DESCONHECIDO. `HandoffIndeterminado` tem handler dedicado
        # (`app/main.py`) com log estruturado e 500 de corpo generico, nunca o
        # 500 cru do Starlette. `IntegrityError` do SAVEPOINT nao chega aqui —
        # o `except` interno, mais proximo, ja o tratou como colisao esperada.
        raise HandoffIndeterminado(
            f"{type(exc).__name__} nao esperado no handoff "
            f"event_id={dados.event_id}: {exc}"
        ) from exc

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
        current_responsavel_id=dados.target_user_id,
    )
