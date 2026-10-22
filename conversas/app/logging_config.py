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

As chamadas `.info` ja existem. Este modulo so instala o handler que faltava.

ATENCAO — o CONTEUDO delas nao foi auditado. A revisao de seguranca desta
mesma task confirmou que varias registram nome, telefone completo e os
primeiros 50 caracteres da mensagem do cliente (`webhook.py:616,626,689`,
`conversations.py:489,1347`, `crm.py:279`, entre outras). Tornar o INFO
observavel e, ao mesmo tempo, exposicao NOVA de PII no log do container.

O `FormatadorSeguro` abaixo resolve apenas a INJECAO de linha de log. Ele
NAO mascara PII, e nao deve ser transformado numa regex generica que tente
adivinhar dado sensivel — filtro por heuristica falha nos dois sentidos.
A reducao de PII exige revisar os call sites da V1 e definir politica de
retencao: ver R11 e DEPLOY-GATE-01 no plano da V2. Nenhum deploy contendo
esta task pode ir a producao antes disso.

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


class FormatadorSeguro(logging.Formatter):
    """Formatter que impede injecao de linha de log por dado do cliente.

    O texto que o cliente manda no WhatsApp aceita quebra de linha, e varias
    chamadas de log interpolam esse texto e o nome de perfil direto numa
    f-string — por exemplo `webhook.py`, ao registrar a mensagem recebida.
    Sem tratamento, uma mensagem contendo \\n permite ao cliente FORJAR uma
    linha inteira dentro do log: basta escrever algo que se pareca com um
    registro legitimo.

    O proprio `webhook.py` ja aplica essa disciplina ao `hub_verify_token`,
    com comentario explicando o risco. A regra existia; nunca tinha sido
    aplicada aos dados do cliente.

    Sanitizar aqui, no formatter, cobre TODAS as chamadas de log do servico
    de uma vez — inclusive as que ainda serao escritas — sem alterar nenhum
    arquivo da V1.

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
            "formatters": {
                "padrao": {
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
    logger.info("logging configurado em %s", nivel_efetivo)
