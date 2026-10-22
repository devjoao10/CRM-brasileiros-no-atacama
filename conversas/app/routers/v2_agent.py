"""
BIA-V2 Fase 10 — `conversas/app/routers/v2_agent.py`.

`POST /webhook/agent-bia-v2`. Fino, por desenho: valida o payload
(`InboundContextV2`, Pydantic com `extra="forbid"`), chama
`app.v2.pipeline.executar_pipeline` e traduz o resultado/exceções em HTTP.
Nenhuma regra de negócio mora aqui — já foi decidida por `pipeline.py` e
pelos módulos das Fases 1-9 que ele orquestra.

Sem prefixo (mesmo padrão de `app/routers/webhook.py`: `router =
APIRouter(tags=[...])`, caminho completo no decorator) — `/webhook/*` é uma
família de rotas, não um recurso `/api/<algo>`.

ROTA SÍNCRONA (`def`, não `async def`) DE PROPÓSITO — exigência explícita da
Fase 10 do plano: a `Session` do SQLAlchemy usada por `executar_pipeline` é
síncrona e bloqueante; dentro de um `async def` ela bloquearia o event loop.
O FastAPI já roda rota síncrona em threadpool (mesmo padrão de
`app/routers/v2_lead_domain.py` no CRM, que documenta a mesma decisão).

MAPEAMENTO HTTP
================
Fixado pelo plano (Fase 10) para os dois primeiros; os demais são decisão
desta fase, registrada aqui e no relatório da tarefa:

  - `EventoDuplicado` -> 409         — handler GLOBAL em `app/main.py`
    (`app.add_exception_handler`), única forma de um handler disparar
    (registrado num `APIRouter` nunca dispara — ver comentário em `main.py`).
  - `CampoInvalido`/`PayloadInvalido` -> 422 — idem, handler GLOBAL.
  - `conversation_id` inexistente -> 404. `ValueError` puro (mesmo idioma de
    `gate.py`/`handoff.py` para erro de chamador) — capturado AQUI, local a
    esta rota, não como handler global: `ValueError` é usado para vários
    erros de chamador distintos em toda a V2, e um handler global demais
    amplo mascararia bugs não relacionados como 404.
  - `ConversationInvalidaParaV2` -> 409 (conflito com o estado atual do
    recurso — a conversa existe, mas está fora do vocabulário que a V2
    processa). Capturada ANTES do `except ValueError` genérico, porque é
    subclasse dele.
  - `HandoffInconsistente` NUNCA chega aqui — `executar_pipeline`
    (`pipeline.py`) já a captura internamente e devolve
    `ResultadoPipeline(EtapaFinal.HANDOFF_INCONSISTENTE, ...)` em vez de
    deixá-la escapar (ver "Outras decisões desta fase" em `pipeline.py`).
    Por isso este router NÃO tem `except HandoffInconsistente`: um clone
    dele aqui seria defesa em profundidade só na aparência — o primeiro (e
    único) interceptador é sempre o try/except DENTRO de `pipeline.py`, e
    isso não muda se o desenho de `event_id` mudar no futuro (aquele except
    não depende de reuso de `event_id` para disparar).

AUTENTICAÇÃO
=============
`Depends(_verificar_auth_webhook)` — achado de segurança corrigido nesta
revisão. A justificativa original ("mesmo padrão de `POST /webhook` (Meta) e
do `/webhook/agent-bia` (V1)") não se sustentou: o webhook da Meta valida
`X-Hub-Signature-256` (HMAC) antes de processar qualquer payload
(`app/routers/webhook.py`), e `/webhook/agent-bia` é hospedado no N8N —
exposto sem autenticação por um achado de auditoria já em remediação
(AUDIT-2026-08-WF2 D2), não um padrão para replicar aqui dentro. Mesmo em
`V2_MODE=dry_run` esta rota já escreve `conversation_events` reais e chama o
Gemini (I/O pago) para qualquer `conversation_id` existente.

Nenhuma credencial já existente serve pronta: `BIA_V2_CRM_API_KEY` (Fase 6) é
a credencial que o CONVERSAS apresenta AO CRM, e `N8N_WEBHOOK_AUTH_HEADER`/
`N8N_WEBHOOK_AUTH_VALUE` (`app/config.py`) é o cabeçalho que o CONVERSAS
manda AO n8n — as duas são OUTBOUND, nenhuma protege uma rota INBOUND deste
próprio app. Mecanismo dedicado: cabeçalho `X-Webhook-Auth` comparado em
tempo constante (`secrets.compare_digest`) contra a nova env var
`V2_AGENT_WEBHOOK_AUTH_VALUE`. Fail-closed — mesma disciplina de
`V2_LEAD_DOMAIN_SERVICE_USER_ID`/`BIA_V2_CRM_API_KEY` (Fase 6): variável
vazia (default) recusa TODA chamada com 401, nunca abre por omissão.
"""
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import V2_AGENT_WEBHOOK_AUTH_VALUE
from app.database import get_db
from app.v2.eventos_validacao import CampoInvalido, PayloadInvalido
from app.v2.pipeline import ConversationInvalidaParaV2, InboundContextV2, ResultadoPipeline, executar_pipeline

router = APIRouter(tags=["V2 - Agente Bia"])

_CABECALHO_WEBHOOK_AUTH = "X-Webhook-Auth"


def _verificar_auth_webhook(x_webhook_auth: str | None = Header(None, alias=_CABECALHO_WEBHOOK_AUTH)) -> None:
    """401 se o cabeçalho não bater com `V2_AGENT_WEBHOOK_AUTH_VALUE`. Ver o
    bloco AUTENTICAÇÃO no docstring do módulo.

    Fail-closed: variável vazia (não configurada) recusa TODA chamada, nunca
    abre por omissão. `x_webhook_auth.isascii()` ANTES de `compare_digest`:
    `secrets.compare_digest` levanta `TypeError` para string não-ASCII, e um
    cabeçalho é entrada do CHAMADOR — sem a checagem, um valor não-ASCII
    viraria 500 em vez de 401 (mesma classe de cuidado que
    `app/v2/handoff.py` já aplica a `BIA_V2_CRM_API_KEY.isascii()`, ainda que
    a exceção concreta lá seja outra). `secrets.compare_digest` em vez de
    `==`: o valor comparado é um segredo, e uma comparação byte-a-byte
    early-exit vazaria o tamanho do prefixo correto por timing.
    """
    valido = (
        bool(V2_AGENT_WEBHOOK_AUTH_VALUE)
        and bool(x_webhook_auth)
        and x_webhook_auth.isascii()
        and secrets.compare_digest(x_webhook_auth, V2_AGENT_WEBHOOK_AUTH_VALUE)
    )
    if not valido:
        raise HTTPException(status_code=401, detail="Credencial invalida")


@router.post("/webhook/agent-bia-v2", dependencies=[Depends(_verificar_auth_webhook)])
def agent_bia_v2(entrada: InboundContextV2, db: Session = Depends(get_db)) -> ResultadoPipeline:
    """Percorre o pipeline V2 inteiro para o payload recebido. Ver o
    docstring do módulo para o mapeamento completo de exceção -> status HTTP."""
    try:
        return executar_pipeline(db, entrada)
    except (CampoInvalido, PayloadInvalido):
        raise  # deixa o handler GLOBAL de app/main.py produzir o 422
    except ConversationInvalidaParaV2 as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
