"""
Configuracao de logging do Conversas — WP BIA-V2, Fase 0, Task 0.1.

POR QUE ISTO EXISTE
-------------------
Sem esta configuracao o root logger fica no default do Python: nivel WARNING
e nenhum handler. O `main.py` nunca chamou `basicConfig`/`dictConfig`, o
Dockerfile sobe `uvicorn app.main:app` sem `--log-config`, e o
`LOGGING_CONFIG` do proprio uvicorn define apenas os loggers `uvicorn`,
`uvicorn.error` e `uvicorn.access` — nunca a chave "root".

Consequencia medida na auditoria: toda a trilha `.info` que o codigo ja
escreve ("Nova conversa criada", "Handoff BIA->humano na conversa X",
"Debounce: enviando N msg(s)", "Resposta da Bia", "Conversa assumida")
nunca sai do processo. Sobrevivem apenas `.warning`/`.error`, sem timestamp
e sem nome do logger.

As chamadas `.info` ja existem e estao bem escritas. Este modulo so instala
o handler que faltava.

ESCOPO — SOMENTE O CONVERSAS
----------------------------
O CRM (`app/`) e o Conversas (`conversas/app/`) sao dois pacotes
independentes que se chamam `app`. Um `from app.logging_config import ...`
resolve para arquivos DIFERENTES em cada processo. Este modulo NAO deve ser
importado pelo CRM: o CRM recebera o seu proprio, duplicado de proposito,
quando hospedar a operacao de dominio da V2 (Fase 6). O logging do CRM para
fins da V1 e HOTFIX-08.
"""
import logging
import os
from logging.config import dictConfig

_FORMATO = "%(asctime)s %(levelname)s %(name)s %(message)s"

logger = logging.getLogger(__name__)


def configurar_logging(nivel: str | None = None) -> None:
    """Instala handler e formatter no root logger.

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
            "formatters": {"padrao": {"format": _FORMATO}},
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
    logger.info("logging configurado em %s", nivel_efetivo)
