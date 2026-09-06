"""
BIA-V2 SOMBRA — `conversas/app/routers/v2_agent_sombra.py`.

`POST /webhook/agent-bia-v2-shadow`. Observacao pura: responde "o que a V2
teria decidido para esta conversa real?" sem NENHUM efeito operacional. O
workflow n8n "Agente Bia" (V1) segue 100% dono do trafego real.

ARQUIVO SEPARADO DE `v2_agent.py`, DE PROPOSITO
================================================
`v2_agent.py` hospeda a rota VIVA (`POST /webhook/agent-bia-v2`), que recusa
conversa `LEGACY` com 409 e cuja semantica nao pode mudar por causa desta
tarefa. Manter a sombra num modulo proprio deixa aquele arquivo byte a byte
identico — um revisor que perguntar "a rota viva mudou?" ve o diff vazio, em
vez de ter de ler um arquivo remexido para concluir a mesma coisa.

AUTENTICACAO — CREDENCIAL PROPRIA, SEPARADA DA ROTA VIVA
===========================================================
Ate uma revisao de seguranca esta rota importava `_verificar_auth_webhook`
de `v2_agent.py` e comparava contra a MESMA `V2_AGENT_WEBHOOK_AUTH_VALUE` da
rota viva. O achado: se o node do n8n que chama a sombra for configurado com
a URL SEM o sufixo `-shadow` (erro de configurar a URL, nao a credencial), a
chamada autentica normalmente contra a rota VIVA — e `executar_pipeline`
(`v2_agent.py` -> `pipeline.py`) ja grava uma linha real de
`MESSAGE_RECEIVED` em `conversation_events` ANTES de recusar `LEGACY` com
409. Toda mensagem de producao espelhada para a sombra por essa URL errada
gravaria uma linha real, sem nenhum efeito visivel ate alguem notar.
Credenciais DIFERENTES fecham essa lacuna: o mesmo erro de URL vira 401
imediato (a sombra nunca aceita a credencial viva, nem vice-versa) em vez de
uma escrita silenciosa.

Por isso esta rota tem sua PROPRIA verificacao (`_verificar_auth_webhook_sombra`,
abaixo) contra sua PROPRIA variavel (`V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE`,
`app/config.py`), em vez de importar o nome privado de `v2_agent.py`. Duas
consequencias, ambas aceitas de proposito:
  - o mecanismo (cabecalho `X-Webhook-Auth`, `secrets.compare_digest`,
    fail-closed, ASCII-guard antes do compare) e REPLICADO aqui, nao
    reimportado — as duas implementacoes so podem divergir por edicao
    humana em cada uma, nunca por definicao compartilhada;
  - `v2_agent.py` fica byte a byte identico: nenhuma promocao de nome
    privado a publico, nenhuma edicao naquele arquivo por causa desta
    tarefa (o achado LOW do import cross-modulo de nome privado tambem
    desaparece, como efeito colateral desta mudanca).

ROTA SINCRONA (`def`, nao `async def`) — mesma razao de `v2_agent.py`: a
`Session` do SQLAlchemy e sincrona e bloqueante, e o FastAPI ja roda rota
sincrona em threadpool.

MAPEAMENTO HTTP — DELIBERADAMENTE MAGRO
=========================================
  - `conversation_id` inexistente -> 404 (`ValueError`, mesmo idioma de
    `pipeline.py`/`gate.py` para erro de chamador).
  - falha do Gemini -> 200. `simular_pipeline` ja converte
    `InterpretacaoFalhou`/`ComunicadorIndisponivel` em desfecho de negocio
    (`INTERPRETACAO_FALHOU`/`COMUNICADOR_INDISPONIVEL`): instabilidade do
    modelo NAO pode virar uma tempestade de 500 numa rota de observacao.
  - `EventoDuplicado`/`CampoInvalido`/`PayloadInvalido` NAO sao tratados aqui,
    e nao por esquecimento: a sombra nao registra evento nenhum, entao nenhuma
    das tres e alcancavel por este caminho. `EntradaSombra` nem tem `event_id`.
  - `EscritaProibidaNaSombra` (`sombra.py`) NAO e capturada: e violacao de
    invariante, nao desfecho de negocio, e precisa aparecer como 500.
"""
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE
from app.database import get_db
from app.v2.sombra import EntradaSombra, ResultadoSombra, simular_pipeline

router = APIRouter(tags=["V2 - Agente Bia (sombra)"])

_CABECALHO_WEBHOOK_AUTH = "X-Webhook-Auth"


def _verificar_auth_webhook_sombra(
    x_webhook_auth: str | None = Header(None, alias=_CABECALHO_WEBHOOK_AUTH),
) -> None:
    """401 se o cabecalho nao bater com `V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE`.
    Ver o bloco AUTENTICACAO no docstring do modulo para o porque desta
    credencial ser PROPRIA e nao a mesma da rota viva.

    Mesma logica de `v2_agent._verificar_auth_webhook`, REPLICADA (nao
    importada) porque as duas rotas agora tem segredos diferentes:

    Fail-closed: variavel vazia (nao configurada) recusa TODA chamada, nunca
    abre por omissao. `x_webhook_auth.isascii()` ANTES de `compare_digest`:
    `secrets.compare_digest` levanta `TypeError` para string nao-ASCII, e um
    cabecalho e entrada do CHAMADOR — sem a checagem, um valor nao-ASCII
    viraria 500 em vez de 401 (mesma classe de cuidado que
    `app/v2/handoff.py` ja aplica a `BIA_V2_CRM_API_KEY.isascii()`, ainda que
    a excecao concreta la seja outra). `secrets.compare_digest` em vez de
    `==`: o valor comparado e um segredo, e uma comparacao byte-a-byte
    early-exit vazaria o tamanho do prefixo correto por timing.
    """
    valido = (
        bool(V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE)
        and bool(x_webhook_auth)
        and x_webhook_auth.isascii()
        and secrets.compare_digest(x_webhook_auth, V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE)
    )
    if not valido:
        raise HTTPException(status_code=401, detail="Credencial invalida")


@router.post("/webhook/agent-bia-v2-shadow", dependencies=[Depends(_verificar_auth_webhook_sombra)])
def agent_bia_v2_shadow(entrada: EntradaSombra, db: Session = Depends(get_db)) -> ResultadoSombra:
    """Simula o pipeline V2 para o payload recebido, sem efeito operacional.
    Ver o docstring do modulo para o mapeamento de exceção -> status HTTP e
    `app/v2/sombra.py` para a garantia estrutural de efeito zero."""
    try:
        return simular_pipeline(db, entrada)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
