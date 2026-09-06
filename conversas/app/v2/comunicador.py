"""
IA Comunicadora - BIA-V2, Fase 9.

`redigir_resposta(entrada)` pede a uma LLM (Gemini) para REDIGIR, em
portugues, o texto de uma mensagem cujo CONTEUDO ja foi decidido pelo
sistema. A IA escolhe COMO dizer; o sistema ja escolheu O QUE (`entrada.acao`
+ os fatos confirmados + os campos faltantes). Nenhum prompt deste modulo
pede a LLM para decidir uma acao - so para redigir uma que ja chegou pronta.

TODO texto que sai desta funcao passa por `filtrar_saida()`
(output_guard.py, mesma Fase 9) ANTES de virar o retorno - nao ha caminho
de retorno que devolva o texto bruto da LLM sem passar pelo guard. Ver
`redigir_resposta` abaixo: as duas saidas possiveis (acao nao autorizada, ou
acao autorizada com texto gerado) chamam `filtrar_saida` exatamente uma vez
cada, nunca retornam antes disso.

CONTRATO DE ENTRADA - `EntradaComunicador`, LOCAL a este modulo
O plano diferiu `AuthorizedResponse` (a entrada desta fase) na Fase 1 porque
`allowed_action` aparece uma UNICA VEZ em todo o corpus, em prosa, sem
vocabulario nenhum - contrato inventado cedo e pior que contrato ausente
(docs/superpowers/plans/2026-08-29-bia-v2.md, linhas 363-377).
`tests/test_v2_contratos.py` (#23) trava que `AuthorizedResponse` ainda NAO
existe em `app.v2.contratos`. Por isso o contrato de entrada desta fase e
LOCAL - mesmo padrao que `MensagemLote` em prefiltro.py (Fase 3), que
tambem nao mora em contratos.py. O NOME e deliberadamente `EntradaComunicador`,
nao `AuthorizedResponse`: evita qualquer leitura de que este modulo estaria
"resolvendo" o subgate de contrato que a Fase 1 deixou pendente - ele nao
esta. Quem rodar aquele subgate no futuro decide o shape real; isto aqui e
so o suficiente para este modulo funcionar hoje.

`fatos_confirmados` reaproveita `TriageData` (Fase 1) em vez de inventar uma
TERCEIRA forma para o mesmo conceito - e exatamente o que a triagem (Fase 2)
ja modela como "fatos que a IA extraiu e confirmou". `campos_faltantes` tem
a MESMA forma de `ResultadoTriagem.campos_faltantes` (Fase 2), pelo mesmo
motivo. `acao` e `StrictStr` sem restricao de vocabulario AQUI - a validacao
contra `output_guard.ACOES_PERMITIDAS` acontece dentro de `redigir_resposta`,
nunca no contrato (mesmo principio de contratos.py: "este modulo so declara
forma, nao decide nada").

R-INTENT TAMBEM VALE PARA `duvidas` (contratos.py:151-157, docstring de
`AIInterpretationResult`): "contar quantas ha, ou repassa-las para a IA
Comunicadora perguntar ao cliente, e uso legitimo; ler o que dizem para
decidir algo e proibido." `duvidas` aqui e OPCIONAL (default `[]`) e so e
usada de DUAS formas em `_montar_prompt` - o COMPRIMENTO da lista (decide
SE a secao de duvidas entra no prompt) e o TEXTO de cada item repassado
VERBATIM para dentro do prompt (dado opaco, nunca inspecionado). Nenhuma
linha deste modulo compara o CONTEUDO de um item de `duvidas` contra
qualquer string, nem ramifica sobre ele - ver a prova de comportamento em
tests/test_v2_comunicador.py (grupo D: duas listas de duvidas com o MESMO
tamanho e CONTEUDO diferente produzem o MESMO veredito).

A CHAMADA A LLM E UM UNICO PONTO ISOLADO - `_chamar_gemini`
Mesmo padrao de `_agora_utc()` em estados.py ("relogio isolado num unico
ponto nomeado, para o teste substituir por unittest.mock.patch"): nenhum
teste deste projeto bate rede de verdade (.claude/rules/05-testing.md).
`redigir_resposta` chama SO essa funcao, nunca `genai` diretamente - o teste
substitui `app.v2.comunicador._chamar_gemini` via `unittest.mock.patch`.

SDK E CONVENCAO - GOOGLE-GENAI, ALINHADO A `interpretador.py` (RECONCILIACAO
POS-FASE 9)
A decisao ORIGINAL desta fase foi espelhar app/routers/ai.py (CRM) e usar
`google.generativeai` (a tarefa pedia seguir a mesma convencao do CRM por
consistencia). A Fase 8 (interpretador.py, mesmo diretorio `app/v2/`) rodou
em paralelo e decidiu, de forma independente e por motivo concreto (SDK
antigo sem suporte a montante - ver o docstring de
`app/v2/interpretador.py`), usar `google-genai`. O resultado eram DOIS SDKs
do Gemini dentro do mesmo subsistema V2 novo para o mesmo tipo de chamada
(texto -> Gemini), o que uma reconciliacao explicita corrigiu: este modulo
agora usa `google-genai` (`from google import genai`), no MESMO padrao ja
verificado por `interpretador.py::_chamar_llm` - cliente instanciado por
chamada (`genai.Client(api_key=...)`), sem `configure()` global (esse metodo
nao existe no SDK novo). NOTA: `app/routers/ai.py` (lado CRM) continua em
`google.generativeai` - decisao de outra fase, fora do escopo desta
reconciliacao, que tocou apenas o subsistema V2 do Conversas.

Diferente de `output_guard.py`, este modulo NAO e puro: chamar uma LLM e
IO de rede por definicao. A pureza que a Fase 9 exige e do GUARD, nao do
comunicador - por isso as duas responsabilidades vivem em arquivos
separados (ver a divisao de responsabilidades no prompt desta tarefa).
"""
import logging

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.config import GEMINI_API_KEY
from app.v2.contratos import ResultadoGuard, TriageData
from app.v2.output_guard import ACOES_PERMITIDAS, filtrar_saida

logger = logging.getLogger(__name__)

# Mesmo modelo usado pelo CRM (app/routers/ai.py) - convencao do projeto.
# Nao configuravel nesta fase: nenhum caller pede modelo diferente (YAGNI).
_MODEL_NAME = "gemini-2.5-flash"

# TETO DE ESPERA DA CHAMADA AO GEMINI - EM MILISSEGUNDOS.
# Mesmo valor e mesma justificativa de `_TIMEOUT_GEMINI_MS` em
# `interpretador.py` (unidade conferida no SDK instalado:
# `google.genai.types.HttpOptions.timeout` e documentado como "Timeout for the
# request in milliseconds", google-genai 1.75.0). Sem ele o SDK espera para
# sempre, e quem chama - caminho vivo e `app/v2/sombra.py` - fica preso junto.
# Duplicado como constante local em vez de importado do interpretador: os dois
# modulos sao fases independentes e nenhum importa o outro hoje.
_TIMEOUT_GEMINI_MS = 90_000


class ComunicadorIndisponivel(RuntimeError):
    """
    Levantada quando `_chamar_gemini` nao consegue produzir texto - chave
    ausente ou falha do SDK/rede. Vocabulario FECHADO de dois tokens (mesmo
    padrao de `TransicaoInvalida` em estados.py); a mensagem NUNCA carrega o
    prompt, os fatos do cliente nem a excecao crua do SDK (essa vai so para
    o log, via `logger.exception`, nunca para a mensagem da excecao).

    Propositalmente NAO e convertida em `ResultadoGuard(permitido=False,...)`
    por `redigir_resposta`: falha de INFRAESTRUTURA (chave ausente, rede
    fora) e um problema DIFERENTE de "o conteudo foi bloqueado por conter
    dinheiro ou usar acao nao autorizada", e um pipeline futuro (Fase 10)
    plausivelmente quer tratar os dois de formas diferentes (retry x nao
    retry). Mesmo espirito de "Fase 8 - Saida invalida ⇒
    AI_INTERPRETATION_FAILED, nunca degradacao": falha deve ser RUIDOSA, nao
    disfarcada de decisao de guard.
    """


_TOKEN_SEM_API_KEY = "gemini_api_key_ausente"
_TOKEN_LLM_FALHOU = "chamada_llm_falhou"


class EntradaComunicador(BaseModel):
    """Contrato de entrada LOCAL desta fase - ver docstring do modulo para
    a provenancia completa e por que o nome NAO e `AuthorizedResponse`."""

    model_config = ConfigDict(extra="forbid")

    acao: StrictStr
    fatos_confirmados: TriageData
    campos_faltantes: list[StrictStr]
    duvidas: list[StrictStr] = Field(default_factory=list)


def _montar_prompt(entrada: EntradaComunicador) -> str:
    """
    Monta o prompt em portugues para a IA Comunicadora. So COMPOE texto -
    nenhuma decisao de negocio acontece aqui, e nenhuma linha le o CONTEUDO
    de `entrada.duvidas` (so o comprimento, para decidir SE a secao entra,
    e o texto de cada item, repassado verbatim - ver docstring do modulo).

    A instrucao "nunca mencione valores monetarios" abaixo e so uma camada
    de BOA VONTADE - NAO e o controle de seguranca. O controle real e
    `filtrar_saida`, chamado incondicionalmente por `redigir_resposta` sobre
    QUALQUER texto que a LLM devolver, inclusive se ela ignorar esta
    instrucao (a Fase 9 existe porque prompt sozinho nao e garantia).
    """
    linhas = [
        "Voce e a assistente de atendimento da Brasileiros no Atacama.",
        "Redija, em portugues do Brasil, uma mensagem curta e cordial para o cliente.",
        "Voce decide APENAS como dizer - o que dizer ja foi decidido pelo sistema.",
        "Nunca mencione valores monetarios, precos, simbolos ou nomes de moeda.",
        "",
        f"Acao a comunicar: {entrada.acao}",
    ]

    # `exclude_defaults=True` e obrigatorio junto com `exclude_none=True`:
    # `TriageData.destinos` tem `default_factory=list` (`[]`, nunca `None`),
    # entao `exclude_none` sozinho NUNCA remove essa chave - toda triagem
    # fresca (nada confirmado ainda) gerava `{'destinos': []}`, um dict NAO
    # vazio, fazendo `if fatos:` disparar e dizer a IA que "destinos vazio"
    # era um FATO CONFIRMADO em vez de "ainda nao perguntado" (achado de
    # revisao de codigo - verificado empiricamente que
    # TriageData().model_dump(exclude_none=True) == {'destinos': []}).
    # Os demais campos de TriageData tem default `None`, ja cobertos por
    # `exclude_none` - `exclude_defaults` so acrescenta a exclusao de
    # `destinos` quando ele ainda vale `[]`, nunca remove um valor
    # realmente confirmado (uma lista de destinos confirmada e sempre
    # NAO-vazia, logo nunca igual ao default).
    fatos = entrada.fatos_confirmados.model_dump(exclude_none=True, exclude_defaults=True)
    if fatos:
        linhas.append(f"Fatos ja confirmados pelo cliente: {fatos}")

    if entrada.campos_faltantes:
        linhas.append(f"Campos que ainda faltam: {', '.join(entrada.campos_faltantes)}")

    # R-INTENT: len() e presenca sao uso legitimo (contar quantas ha); o
    # texto de cada duvida entra verbatim, como dado opaco, nunca inspecionado.
    if entrada.duvidas:
        linhas.append(
            f"O cliente levantou {len(entrada.duvidas)} duvida(s) - "
            "ofereca esclarecer, sem presumir o que elas significam:"
        )
        for duvida in entrada.duvidas:
            linhas.append(f"- {duvida}")

    return "\n".join(linhas)


def _chamar_gemini(prompt: str) -> str:
    """
    Unico ponto que toca a rede (Gemini) - isolado e nomeado para o teste
    substituir via `unittest.mock.patch("app.v2.comunicador._chamar_gemini",
    ...)`, mesmo padrao de `_agora_utc()` em estados.py. Nunca loga `prompt`
    (carrega fatos do cliente) nem qualquer excecao crua do SDK na MENSAGEM
    de `ComunicadorIndisponivel` - so no `logger.exception`, server-side.

    SDK `google-genai`, mesmo padrao de `interpretador.py::_chamar_llm`:
    cliente instanciado por chamada, sem `config=` (este modulo nao pede
    saida estruturada contra um schema Pydantic - so texto livre, que
    `redigir_resposta` passa em seguida por `filtrar_saida`).
    """
    if not GEMINI_API_KEY:
        raise ComunicadorIndisponivel(_TOKEN_SEM_API_KEY)
    try:
        cliente = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=_TIMEOUT_GEMINI_MS),
        )
        resposta = cliente.models.generate_content(model=_MODEL_NAME, contents=prompt)
        if not resposta.text:
            raise RuntimeError("Resposta vazia do modelo de IA.")
        return resposta.text
    except Exception:
        logger.exception("[COMUNICADOR] chamada ao Gemini falhou")
        raise ComunicadorIndisponivel(_TOKEN_LLM_FALHOU) from None


def redigir_resposta(entrada: EntradaComunicador) -> ResultadoGuard:
    """
    Ponto de entrada da IA Comunicadora (Fase 9).

    ORDEM (decisao documentada aqui, ja que o plano permite as duas — ver o
    prompt desta tarefa): acao nao autorizada e recusada ANTES de gastar uma
    chamada de rede com a LLM (fail-fast). `filtrar_saida` continua sendo a
    UNICA fonte do FORMATO da recusa (motivo/texto_final) - esta funcao nao
    duplica esse formato, so evita o custo da chamada quando o resultado ja
    e conhecido de antemao. Quando a acao E autorizada, o texto gerado
    SEMPRE passa por `filtrar_saida` antes de retornar - nunca ha um
    caminho que devolva o texto cru da LLM.
    """
    if entrada.acao not in ACOES_PERMITIDAS:
        return filtrar_saida("", acao=entrada.acao)

    prompt = _montar_prompt(entrada)
    texto_bruto = _chamar_gemini(prompt)
    return filtrar_saida(texto_bruto, acao=entrada.acao)
