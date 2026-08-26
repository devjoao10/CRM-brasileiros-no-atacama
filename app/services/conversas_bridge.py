"""
AUDIT-2026-08-WA — a ponte que faltava entre a decisao do handoff e a fila.

O PROBLEMA
----------
`POST /api/conversations/{id}/handoff` existe no Conversas, esta correto, e
**nao tem chamador**. Os 18 nos do workflow "Agente Gerenciador de Leads"
apontam todos para `http://crm:8000/...`; nenhum alcanca a porta 8001
(verificado nos exports de 2026-08-26). Resultado: `is_bot_active` nunca virava
False e `queued_at` nunca era preenchido — a conversa ficava em ATENDIMENTOS
BIA enquanto a Bia dizia ao cliente que ele estava na fila.

O UNICO sinal deterministico que o repositorio recebe no momento do handoff e
`PUT /api/leads/{lead_id}/responsavel`, que o Gerenciador chama pela ferramenta
`Tool Alterar Responsavel`. E dai que esta ponte parte.

POR QUE HTTP, E NAO SQL DIRETO
------------------------------
Os dois servicos compartilham o banco, e seria mais curto dar um UPDATE em
`conversations` daqui. Nao: `conversations` pertence ao Conversas, e o proprio
ROOT-001 desta auditoria nomeia "escrever na tabela do outro servico com SQL
cru" como a causa de uma familia inteira de bugs — cada invariante do dono
precisa ser reimplementado ou e pulado. A transicao de estado da conversa tem
regra (resolucao de atendente elegivel, preservacao da posicao na fila,
idempotencia do retry) e essa regra mora no dono. Chamamos a rota dele.

`CONVERSAS_BASE_URL` ja existia na config e nunca havia sido usado por nada.

BEST-EFFORT, POR DESENHO
------------------------
Esta funcao **nunca levanta**. Se o Conversas estiver fora do ar, se a
credencial nao estiver configurada, se nao houver conversa aberta para o lead —
a troca de responsavel no CRM continua valendo e a resposta diz o que
aconteceu. O contrario (derrubar o `PUT` do n8n porque o inbox esta reiniciando)
transformaria uma degradacao em perda de dado do lead.
"""
import logging
from typing import Optional

import httpx

from app.config import CONVERSAS_API_KEY, CONVERSAS_BASE_URL

logger = logging.getLogger(__name__)

# Curto de proposito: isto roda DENTRO do request que o n8n esta esperando.
# O handoff e uma escrita pequena numa tabela pequena; se demorar mais que isso,
# alguma coisa esta errada e e melhor devolver o lead salvo do que segurar o n8n.
_TIMEOUT_SEGUNDOS = 5.0


async def notificar_handoff(lead_id: int) -> Optional[bool]:
    """
    Avisa o Conversas que o lead entrou na fila humana.

    Devolve:
      True   — a conversa foi movida para a fila;
      False  — o Conversas respondeu, mas nao havia conversa aberta (404) ou
               recusou a chamada;
      None   — a ponte nao esta configurada, ou o Conversas nao respondeu.

    O chamador expoe isso ao cliente para que uma falha silenciosa nao se
    disfarce de sucesso — foi assim que o handoff quebrado passou despercebido.
    """
    if not CONVERSAS_API_KEY:
        # Sem credencial a ponte e no-op silencioso: e o comportamento de hoje
        # (nada acontecia) e nao queremos ruido de log a cada lead em dev.
        logger.debug(
            "Ponte CRM->Conversas nao configurada (CONVERSAS_API_KEY vazia); "
            "handoff do lead %s nao propagado.", lead_id
        )
        return None

    url = f"{CONVERSAS_BASE_URL.rstrip('/')}/api/conversations/by-lead/{lead_id}/handoff"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEGUNDOS) as client:
            resp = await client.post(url, headers={"X-API-Key": CONVERSAS_API_KEY})
    except Exception as exc:  # noqa: BLE001 — qualquer falha de rede e degradacao, nao erro do lead
        logger.warning(
            "Ponte CRM->Conversas falhou para o lead %s (%s: %s). "
            "O responsavel foi salvo; a conversa NAO foi movida para a fila.",
            lead_id, type(exc).__name__, exc,
        )
        return None

    if resp.status_code == 200:
        logger.info("Handoff propagado ao Conversas: lead %s entrou na fila humana.", lead_id)
        return True

    if resp.status_code == 404:
        # Normal: lead sem conversa de WhatsApp aberta (veio de formulario, por
        # exemplo). Nao e falha — nao ha fila para onde mover.
        logger.info("Lead %s nao tem conversa aberta; nada a mover para a fila.", lead_id)
        return False

    logger.warning(
        "Conversas recusou o handoff do lead %s (HTTP %s). O responsavel foi salvo.",
        lead_id, resp.status_code,
    )
    return False
