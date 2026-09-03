"""
IA Interpretadora — BIA-V2, Fase 8.

`interpretar()` chama o modelo de linguagem para transformar UMA mensagem do
cliente em fatos estruturados: um `AIInterpretationResult` (contrato da Fase
1, `app/v2/contratos.py`). So FATOS interpretados saem daqui — nenhuma
decisao operacional. Fila, handoff, responsavel ou proximo estado nao sao
resolvidos neste modulo.

SEM TOOLS DE ESCRITA (exigencia do plano). O `GenerateContentConfig` montado
em `_chamar_llm` nunca declara `tools=`, entao o modelo nao tem nenhuma
ferramenta de function-calling disponivel — nem de escrita, nem de leitura.
Tools de KB read-only (permitidas pelo plano) NAO estao implementadas nesta
rodada: nenhum teste critico desta fase exige leitura de base de
conhecimento, e integrar sem consumidor real seria especular. Quando uma
fase futura precisar, ela acrescenta `tools=[...]` em `_chamar_llm` — sempre
funcoes que so leem.

SAIDA INVALIDA NUNCA DEGRADA (exigencia do plano). Se a resposta do modelo
nao bater EXATAMENTE com `AIInterpretationResult` — JSON malformado, campo
obrigatorio faltando, campo extra (`extra="forbid"`), tipo errado — a funcao
NUNCA devolve um `AIInterpretationResult` parcial ou vazio como substituto
silencioso. Ela grava `AI_INTERPRETATION_FAILED` e levanta
`InterpretacaoFalhou`. Uma excecao propria, de um tipo que `interpretar()`
jamais devolve no caminho de sucesso, torna estruturalmente impossivel um
chamador confundir falha com sucesso.

FRONTEIRA DE I/O ISOLADA: `_chamar_llm()` e a UNICA funcao deste modulo que
fala com o SDK do Gemini (rede, credencial, latencia, nao-determinismo). Ela
devolve uma STRING crua (o `.text` da resposta) e nao interpreta o
conteudo — quem valida e `interpretar()`, via
`AIInterpretationResult.model_validate_json`. Testes substituem esta funcao
inteira (`interpretador._chamar_llm = fake`), o mesmo padrao ja usado em
`conversas/app/services/whatsapp.send_text_message` nos testes de agente
(`tests/test_conversas_agent_timeout.py`) — nenhum teste desta fase bate
rede real.

SDK — DIVERGENCIA DELIBERADA de `app/services/ai_tools.py` e
`app/routers/ai.py` (CRM), que usam `google.generativeai`. Este modulo usa
`google-genai` (`from google import genai`) em vez disso. Motivo concreto,
nao preferencia:

  1. `google-generativeai` esta MORTO a montante. Instalando a versao que
     satisfaz o pin do CRM (`google-generativeai>=0.8.2`, resolvido hoje
     para 0.8.6) e importando o pacote, a PROPRIA biblioteca emite
     `FutureWarning`: "All support for the `google.generativeai` package
     has ended. It will no longer be receiving updates or bug fixes."
     (verificado empiricamente, nao por leitura de changelog). Construir
     codigo NOVO — Fase 8 de uma migracao que existe para corrigir divida
     tecnica — sobre um pacote sem mais correcao de bug nem patch de
     seguranca nao se justifica com o sucessor oficial disponivel e estavel.
  2. `google-genai` usa `httpx` como cliente HTTP por baixo, alinhado com a
     convencao ja estabelecida do Conversas ("httpx e o cliente HTTP do
     subsistema", `app/services/CLAUDE.md`). `google-generativeai` traz
     `grpcio` + `google-api-python-client`, arvore de dependencia pesada e
     desalinhada do resto do Conversas.
  3. A CONVENCAO pedida (nome de SDK/padrao de configuracao do CRM) e
     preservada onde importa: mesmo nome de env var (`GEMINI_API_KEY`),
     mesmo modelo (`gemini-2.5-flash`), e o mesmo padrao de saida
     estruturada documentado pela Google para as duas familias de SDK —
     `GenerateContentConfig(response_mime_type="application/json",
     response_schema=<classe Pydantic>)`. So o nome do pacote muda.

VERSAO FIXADA EM `google-genai==1.75.0`, nao a mais recente (2.22.0 no
momento desta fase). A partir da serie 2.x o pacote exige
`pydantic>=2.12.5`, acima do `pydantic==2.11.2` fixado em
`conversas/requirements.txt` — instalar a versao mais nova forcaria upgrade
de pydantic para o Conversas inteiro, fora do escopo desta fase. `1.75.0` e
a ultima release da serie 1.x (`pydantic<3.0.0,>=2.9.0`, compativel) e foi
confirmada por introspeccao real (nao suposta): `genai.Client.__init__`,
`Models.generate_content` e os campos de `GenerateContentConfig` usados
abaixo tem exatamente a assinatura empregada aqui, e a conversao do
`AIInterpretationResult` REAL deste projeto (com `TriageData`/
`DestinoTriagem` aninhados, `StrictStr`/`StrictBool`, `extra="forbid"`) para
o schema do Gemini foi exercitada de ponta a ponta ate a camada de
transporte HTTP sem erro, num teste manual com a rede bloqueada de
proposito logo antes do envio — nenhum teste AUTOMATIZADO desta suite
depende de rede, esse teste manual so validou a integracao do tipo antes de
escrever este arquivo.

NOTA DE DEPENDENCIA (pyasn1 x python-jose): instalar `google-genai` traz
`google-auth`, que exige `pyasn1>=0.6.1`; `python-jose[cryptography]==3.4.0`
(auth do Conversas) declara `pyasn1<0.5.0`. `pip check` acusa o conflito de
versao. Verificado manualmente: o Conversas assina tokens exclusivamente
com HS256 (`app/config.py: ALGORITHM = "HS256"`), que e HMAC puro e nao
toca nenhum caminho de codigo ASN.1 — `jose.jwt.encode`/`decode` com HS256
foram testados de ponta a ponta neste ambiente com `pyasn1==0.6.4`
instalado (o que `google-auth` forca), sem falha. O conflito de versao
declarado e real, mas inerte para o uso que este projeto faz de
`python-jose`; tambem nao e especifico desta escolha de SDK — qualquer
biblioteca cliente da Google (`google-generativeai` inclusive) traz o mesmo
`google-auth` e o mesmo piso de `pyasn1`.

MODEL_ATTEMPT E 0-BASED — decisao desta fase, pendente desde
`eventos.py`/`eventos_validacao.py` ("Fase 8 ... precisa decidir e
documentar a convencao antes de gravar este campo"). A primeira tentativa
de interpretar uma mensagem grava `model_attempt=0`. Este modulo nao
implementa retry (nenhum consumidor exige isso ainda); o parametro existe
para quem chamar `interpretar()` mais de uma vez para a MESMA mensagem
poder numerar as tentativas.

CICLO DE VIDA DE EVENTOS — decisao desta fase: SIM, emite os tres.
`AI_INTERPRETATION_STARTED` e gravado antes de chamar o modelo;
exatamente UM entre `AI_INTERPRETATION_COMPLETED` (sucesso) e
`AI_INTERPRETATION_FAILED` (qualquer outro desfecho) e gravado ao final —
nunca os dois. Motivo: sao os 3 unicos tipos com prefixo
`AI_INTERPRETATION_` no vocabulario fechado de `TipoEvento`
(`eventos_validacao.py`), nenhuma outra fase os menciona, e sem STARTED a
tabela de eventos nunca distinguiria "interpretacao em andamento" de
"nunca comecou" ao investigar uma conversa travada. `duration_ms` mede
`time.monotonic()` entre o inicio da chamada ao modelo e o desfecho —
sucesso ou falha.

R-INTENT (ver Fase 1): nenhuma decisao deste modulo pode ramificar sobre o
texto livre de `AIInterpretationResult.intent`/`.duvidas`. Este modulo e
justamente quem PRODUZ esses campos — ele os devolve intactos dentro do
objeto validado, nunca le o conteudo deles para decidir nada.
`tests/test_v2_interpretador.py` prova a ausencia via AST, no mesmo
espirito de `tests/test_v2_r_intent.py` (que tambem varre este arquivo,
por estar sob `conversas/app/v2/**/*.py`).
"""
import time

from google import genai
from google.genai import types
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import GEMINI_API_KEY
from app.v2.contratos import AIInterpretationResult
from app.v2.eventos import registrar_evento
from app.v2.eventos_validacao import ResultadoEvento, TipoEvento

# Mesmo modelo usado hoje pelo CRM (app/routers/ai.py) — sem motivo para
# divergir aqui, so o pacote do SDK mudou (ver docstring do modulo).
NOME_MODELO = "gemini-2.5-flash"

# Vocabulario fechado de error_code desta fase (formato exigido por
# eventos_validacao._REGEX_ERROR_CODE: maiuscula inicial, so [A-Z0-9_]).
_ERRO_CHAMADA_LLM = "CHAMADA_LLM_FALHOU"
_ERRO_RESPOSTA_INVALIDA = "RESPOSTA_INVALIDA"

# NAO descreve o JSON schema de AIInterpretationResult em prosa aqui: a
# documentacao oficial do google-genai recomenda explicitamente NAO duplicar
# o schema (ou exemplos do formato esperado) no prompt quando response_schema
# ja fixa a forma — duplicar degrada a qualidade da saida.
_INSTRUCAO_SISTEMA = (
    "Voce e a camada de interpretacao de uma agente de viagens brasileira "
    "especializada no Atacama (Bia). Sua UNICA tarefa e ler a mensagem mais "
    "recente do cliente e extrair fatos estruturados. Voce NUNCA decide "
    "fila, encaminhamento, proximo estado ou o que responder ao cliente — "
    "isso e feito por outro sistema, deterministico, depois de voce.\n\n"
    "Preencha `extracted` so com o que a mensagem realmente informa; campo "
    "nao mencionado fica omitido, nunca inventado.\n"
    "`explicit_human_request` e verdadeiro somente quando o cliente pede "
    "EXPLICITAMENTE para falar com uma pessoa/atendente/humano — duvida "
    "comum sobre a viagem nao conta.\n"
    "`duvidas` lista, em portugues, as perguntas ou incertezas do cliente "
    "ainda sem resposta nesta mensagem; lista vazia se nao houver nenhuma.\n"
    "`intent` e uma frase curta e livre descrevendo a intencao da "
    "mensagem — observacao linguistica, sem vocabulario fixo."
)


class InterpretacaoFalhou(Exception):
    """
    A interpretacao desta mensagem NAO produziu um `AIInterpretationResult`
    valido — por falha na chamada ao modelo (`error_code=CHAMADA_LLM_FALHOU`)
    ou por saida que nao validou contra o contrato
    (`error_code=RESPOSTA_INVALIDA`). `interpretar()` nunca devolve um
    resultado parcial: ou volta um `AIInterpretationResult` completo e
    valido, ou levanta esta excecao — nao existe terceiro valor que um
    chamador possa confundir com sucesso.

    DECISAO CONSCIENTE (revisao de codigo, Fase 9): esta excecao preserva a
    excecao original do SDK como `__cause__` (`raise ... from exc`, ver
    `_chamar_llm`/`interpretar` abaixo) — divergente de proposito do padrao
    de `comunicador.py::ComunicadorIndisponivel`, que usa `from None` +
    `logger.exception()` so no servidor, inspirado num bug real ja corrigido
    no CRM (AUDIT-2026-08-W1C: `str(e)` do SDK vazando de volta pro contexto
    do modelo). Aqui a cadeia fica visivel de proposito PORQUE nada consome
    `InterpretacaoFalhou` ainda (Fase 10 nao existe) e o teste desta fase
    depende de inspecionar `exc.__cause__` pra provar que o erro real do SDK
    nao foi mascarado. QUEM IMPLEMENTAR A FASE 10 (o endpoint/pipeline) NAO
    PODE logar ou serializar esta excecao com `traceback`/`logger.exception`
    completo em qualquer caminho que alcance um sistema externo (APM, log
    agregado, resposta HTTP) sem antes revisar se `__cause__` carrega algo
    sensivel do SDK — ou alinhar este modulo ao padrao `from None` do
    comunicador nesse momento, o que exigiria reescrever o teste que checa
    `exc.__cause__` hoje.
    """

    def __init__(self, mensagem: str, *, error_code: str):
        self.error_code = error_code
        super().__init__(mensagem)


def _chamar_llm(mensagem: str) -> str:
    """
    FRONTEIRA DE I/O — unica funcao deste modulo que fala com o SDK do
    Gemini. Devolve o `.text` cru da resposta (esperado ser uma string
    JSON que bate com `AIInterpretationResult`); NAO interpreta nem valida
    o conteudo, so entrega.

    Levanta `RuntimeError` se a credencial nao estiver configurada ou se o
    modelo devolver uma resposta sem texto (bloqueio de seguranca do
    proprio Gemini, resposta vazia etc.). `interpretar()` trata QUALQUER
    excecao vinda desta funcao como falha de chamada, sem distinguir o
    motivo — mesmo padrao de `app/routers/ai.py` (CRM) ao redor de chamadas
    ao SDK do Gemini.

    Testes substituem esta funcao inteira
    (`interpretador._chamar_llm = fake`), nunca o cliente por dentro.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY nao configurada — a IA Interpretadora nao pode ser chamada."
        )

    cliente = genai.Client(api_key=GEMINI_API_KEY)
    resposta = cliente.models.generate_content(
        model=NOME_MODELO,
        contents=mensagem,
        config=types.GenerateContentConfig(
            system_instruction=_INSTRUCAO_SISTEMA,
            response_mime_type="application/json",
            response_schema=AIInterpretationResult,
            # SEM tools=: nenhuma ferramenta (escrita ou leitura) e
            # declarada nesta chamada — exigencia do plano para a Fase 8.
        ),
    )
    if not resposta.text:
        raise RuntimeError("Resposta vazia do modelo de IA.")
    return resposta.text


def interpretar(
    db: Session,
    *,
    mensagem: str,
    commit: bool,
    conversation_id: int | None = None,
    lead_id: int | None = None,
    model_attempt: int = 0,
) -> AIInterpretationResult:
    """
    Interpreta UMA mensagem do cliente, devolvendo um `AIInterpretationResult`
    validado. `commit` e keyword-only sem default — mesmo padrao de
    `registrar_evento`: todo caller declara explicitamente quem controla a
    transacao.

    Emite os 3 eventos do ciclo de vida de interpretacao (ver docstring do
    modulo): STARTED antes de chamar o modelo, e exatamente UM entre
    COMPLETED (sucesso) e FAILED (qualquer falha) ao final.

    Levanta `InterpretacaoFalhou` — nunca devolve um resultado parcial — se
    a chamada ao modelo falhar ou se a resposta nao validar contra
    `AIInterpretationResult`.
    """
    registrar_evento(
        db,
        tipo=TipoEvento.AI_INTERPRETATION_STARTED,
        commit=commit,
        conversation_id=conversation_id,
        lead_id=lead_id,
        model=NOME_MODELO,
        model_attempt=model_attempt,
    )

    inicio = time.monotonic()

    try:
        texto_bruto = _chamar_llm(mensagem)
    except Exception as exc:
        # Ampla de proposito: qualquer falha da fronteira de I/O (rede,
        # credencial ausente, erro do SDK, bloqueio de seguranca do modelo)
        # e a MESMA falha do ponto de vista deste modulo — "a interpretacao
        # nao aconteceu".
        duracao_ms = int((time.monotonic() - inicio) * 1000)
        registrar_evento(
            db,
            tipo=TipoEvento.AI_INTERPRETATION_FAILED,
            commit=commit,
            conversation_id=conversation_id,
            lead_id=lead_id,
            model=NOME_MODELO,
            model_attempt=model_attempt,
            duration_ms=duracao_ms,
            result=ResultadoEvento.FALHA,
            error_code=_ERRO_CHAMADA_LLM,
        )
        raise InterpretacaoFalhou(
            "Falha ao chamar o modelo de IA.", error_code=_ERRO_CHAMADA_LLM
        ) from exc

    try:
        resultado = AIInterpretationResult.model_validate_json(texto_bruto)
    except ValidationError as exc:
        # Cobre JSON malformado, campo obrigatorio faltando, campo extra
        # (extra="forbid") e tipo errado — Pydantic v2 emite ValidationError
        # para os quatro casos via model_validate_json (confirmado
        # empiricamente contra o AIInterpretationResult real deste projeto
        # antes de escrever este arquivo).
        duracao_ms = int((time.monotonic() - inicio) * 1000)
        registrar_evento(
            db,
            tipo=TipoEvento.AI_INTERPRETATION_FAILED,
            commit=commit,
            conversation_id=conversation_id,
            lead_id=lead_id,
            model=NOME_MODELO,
            model_attempt=model_attempt,
            duration_ms=duracao_ms,
            result=ResultadoEvento.FALHA,
            error_code=_ERRO_RESPOSTA_INVALIDA,
        )
        raise InterpretacaoFalhou(
            "Saida do modelo de IA nao bate com AIInterpretationResult.",
            error_code=_ERRO_RESPOSTA_INVALIDA,
        ) from exc

    duracao_ms = int((time.monotonic() - inicio) * 1000)
    registrar_evento(
        db,
        tipo=TipoEvento.AI_INTERPRETATION_COMPLETED,
        commit=commit,
        conversation_id=conversation_id,
        lead_id=lead_id,
        model=NOME_MODELO,
        model_attempt=model_attempt,
        duration_ms=duracao_ms,
        result=ResultadoEvento.SUCESSO,
    )
    return resultado
