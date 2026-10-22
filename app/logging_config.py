"""
Configuracao de logging do CRM — WP BIA-V2, Fase 6 (HOTFIX-08).

POR QUE ISTO EXISTE
-------------------
Mesma falha que a Fase 0 corrigiu no Conversas, ainda aberta no CRM: sem esta
configuracao o root logger fica no default do Python (nivel WARNING, nenhum
handler). `app/main.py` nunca chamou `basicConfig`/`dictConfig`, o Dockerfile
sobe `uvicorn app.main:app` sem `--log-config`, e o `LOGGING_CONFIG` do proprio
uvicorn define apenas os loggers `uvicorn`, `uvicorn.error` e `uvicorn.access`
— nunca a chave "root".

Consequencia: toda a trilha `.info` que o CRM ja escreve morre dentro do
processo (por exemplo `app/routers/leads.py`, que registra o resultado da ponte
de handoff para o Conversas, e `app/main.py`, que registra a limpeza de
uploads). Sobrevivem apenas `.warning`/`.error`, sem timestamp e sem nome do
logger. A Fase 6 passa a hospedar `app/routers/v2_lead_domain.py` no CRM — a
operacao comercial idempotente do handoff — e sem handler no root nada do que
esse endpoint registra sairia do container.

POR QUE E DUPLICADO, E NAO IMPORTADO DO CONVERSAS
------------------------------------------------
O CRM (`app/`) e o Conversas (`conversas/app/`) sao dois pacotes independentes
que se chamam `app`. Um `from app.logging_config import ...` resolve para
arquivos DIFERENTES em cada processo, e os dois rodam com `requirements.txt` e
versoes de Python distintas. Compartilhar exigiria mudar a fronteira de
pacotes, o que esta fora do escopo da V2 — a duplicacao e deliberada e esta
registrada como tal no plano (Fase 0, secao "Escopo"). Este modulo NAO deve ser
importado pelo Conversas, nem o do Conversas por aqui.

ATENCAO — o CONTEUDO das chamadas de log do CRM nao foi auditado por esta fase.
Tornar o INFO observavel e ganho de diagnostico e, ao mesmo tempo, exposicao
nova do que quer que essas chamadas ja escrevam. O equivalente do lado do
Conversas esta registrado como R11/DEPLOY-GATE-01 no plano da V2; a varredura
do lado do CRM (`grep -rn "\\.info(\\|\\.debug(" app/`) e revisao caso a caso
por call site, nao problema resolvivel por regex neste modulo — ver
`FormatadorSeguro` abaixo.
"""
import logging
import os
from logging.config import dictConfig

_FORMATO = "%(asctime)s %(levelname)s %(name)s %(message)s"

logger = logging.getLogger(__name__)


class FormatadorSeguro(logging.Formatter):
    """Formatter que impede injecao de linha de log por dado nao confiavel.

    Nao e simetria decorativa com o Conversas: o CRM tem call site concreto
    hoje. `app/services/ai_tools.py:383,397` registra `query[:200]` — a query
    SQL que a IA GEROU a partir de texto que chegou do WhatsApp via n8n —
    quando a bloqueia. String de SQL contem quebra de linha por construcao, e
    a query e influenciada por quem escreve no WhatsApp: sem tratamento, uma
    tentativa bloqueada pode FORJAR uma linha inteira dentro do log, bastando
    que o texto injetado se pareca com um registro legitimo. Justamente o log
    de seguranca — o que registra o bloqueio — e o que se pode falsificar.

    Sanitizar no formatter cobre TODAS as chamadas de log do servico de uma
    vez, inclusive as que ainda serao escritas, sem alterar nenhum arquivo da
    V1.

    O que este formatter NAO faz: mascarar PII. Isso e semantico e nao tem
    resposta deterministica por regex — falharia por falso negativo (formato
    nao previsto passa direto, e a equipe passa a confiar num filtro que nao
    filtra) e por falso positivo (um id legitimo vira `***`, destruindo o
    diagnostico). O lugar certo e o call site, onde se sabe o que a variavel
    significa. Nao transforme isto numa heuristica.

    Sobrescreve `formatMessage`, e nao `format`, de proposito: `format` roda
    depois e e quem anexa o traceback de `exc_info`. Sanitizar em `format`
    espremeria o traceback inteiro numa linha so, destruindo justamente a
    informacao mais util num erro. Em `formatMessage` a mensagem vira uma
    linha e o traceback continua multilinha.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        record.message = record.message.replace("\r", "\\r").replace("\n", "\\n")
        return super().formatMessage(record)


def configurar_logging(nivel: str | None = None) -> None:
    """Instala handler e formatter no root logger do CRM.

    Idempotente por construcao: `dictConfig` com a chave "root" SUBSTITUI a
    lista de handlers do root em vez de acrescentar a ela, entao chamar duas
    vezes nao duplica linha de log.

    `disable_existing_loggers=False` preserva os loggers que o uvicorn ja
    criou — sem isso, os logs de acesso do proprio servidor emudeceriam.

    Args:
        nivel: nivel do root logger. Quando None, usa a env var LOG_LEVEL,
            e na ausencia dela "INFO".
    """
    nivel_efetivo = (nivel or os.getenv("LOG_LEVEL") or "INFO").upper()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "padrao": {
                    # Resolve para ESTE modulo no processo do CRM — o pacote
                    # `app` do Conversas nunca esta no sys.path aqui.
                    "()": "app.logging_config.FormatadorSeguro",
                    "format": _FORMATO,
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "padrao",
                }
            },
            "root": {"handlers": ["stdout"], "level": nivel_efetivo},
        }
    )
    logger.info("logging do CRM configurado em %s", nivel_efetivo)
