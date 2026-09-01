"""
Contratos Pydantic da V2 — BIA-V2, Fase 1.

ESCOPO: 6 modelos, nao 15
A lista original da Fase 1 tinha 15. O subgate de reconciliacao levantou a
provenance de cada um e achou shape aprovado para poucos: a maioria aparecia
UMA UNICA VEZ no corpus, na propria lista, sem campo, tipo ou operacao que os
fixasse. Os outros 9 ficaram com a fase que realmente os consome, cada uma
abrindo com subgate de contrato — ver a tabela na Fase 1 do plano
(docs/superpowers/plans/2026-08-29-bia-v2.md).

`DomainEvent` foi REMOVIDO, nao diferido: a Task 0.2 ja criou a fonte de
verdade de eventos (`ConversationEvent` ORM, `registrar_evento()`,
`TipoEvento`, `ResultadoEvento`, `OrigemEvento`), e nenhuma interface consome
um DTO Pydantic de evento.

ESTE MODULO SO DECLARA FORMA. NAO DECIDE NADA.
Nenhum validator de negocio mora aqui. Completude de triagem e da Fase 2;
vocabulario de motivo do pre-filtro e da Fase 3; maquina de estados e da
Fase 4; handoff e da Fase 6; guard de saida e da Fase 9. Um schema que
comeca a decidir vira um segundo motor de regras escondido — e a arquitetura
inteira desta V2 existe porque decisao espalhada por camadas que nao a
declaram foi exatamente o que quebrou na V1.

INERCIA DE IMPORT — propriedade, nao coincidencia
Importar este modulo carrega `pydantic` e `datetime`, e nada mais. Sem
SQLAlchemy, sem `app.database`, sem `app.models`, sem httpx, sem CRM, sem
n8n. Em particular NAO importa `app.v2.eventos`, que arrasta o model, o
`database` e o `config` (este ultimo levanta `RuntimeError` sem `SECRET_KEY`
fora de development). Os enums de evento tambem nao sao importados: nenhum
dos 6 modelos os usa, e import nao utilizado e acoplamento antecipado. Se um
dia forem precisos, vem de `app.v2.eventos_validacao` — stdlib puro, sem
ciclo —, nunca de `eventos.py`. Travado por teste.

TIPOS ESTRITOS
`StrictStr`/`StrictInt`/`StrictBool` sao deliberados: sem eles o Pydantic
aceita `True` onde se espera inteiro (e grava 1), e `"5"` onde se espera
numero. Num contrato que valida saida de LLM, coercao silenciosa e o comeco
do mesmo problema que o `pronto_para_humano` causou — dado malformado
atravessando camadas sem gerar erro. Nao ha `ConfigDict(strict=True)`
global: `date` continua aceitando string ISO, que e como um JSON de LLM
representa data.
"""
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class _ContratoV2(BaseModel):
    """Base dos contratos da V2. So carrega config — nenhum comportamento.

    `extra="forbid"` e o que transforma "campo desconhecido" em erro em vez
    de silencio. E allowlist estrutural, nao denylist de nomes: um campo
    operacional inventado sob qualquer nome e rejeitado por nao estar
    declarado, sem depender de alguem ter previsto aquele nome.
    """

    model_config = ConfigDict(extra="forbid")


class DestinoTriagem(_ContratoV2):
    """Fatos de UM destino da viagem.

    `data_inicio`/`data_fim`/`dias` sao todos opcionais DE PROPOSITO. A regra
    aprovada — todos os destinos com datas, ou todos com dias, nunca modo
    misto — e da Fase 2 (`avaliar_triagem`). Aqui um destino com nome e mais
    nada e estruturalmente valido: e o que a IA extrai na primeira mensagem.

    `date`, nao `datetime`: data de viagem e data civil. Hora e timezone nao
    fazem parte do fato. Timestamps operacionais seguem `datetime` nos outros
    dominios.
    """

    destino: StrictStr
    data_inicio: date | None = None
    data_fim: date | None = None
    dias: StrictInt | None = None


class TriageData(_ContratoV2):
    """Fatos de triagem que a IA extrai. TODO campo e opcional.

    Nao e descuido: a extracao e INCREMENTAL. A primeira mensagem raramente
    traz nome, email, destinos e contagem juntos. Um campo obrigatorio aqui
    impediria o objeto parcial de existir — e `avaliar_triagem()`, cuja
    funcao inteira e dizer O QUE FALTA, nunca receberia o parcial que ela
    existe para avaliar. `campos_faltantes` ficaria inalcancavel.

    String vazia NAO vira `None`. Se a Fase 2 decidir que vazio conta como
    ausente, e decisao dela, tomada com o contexto dela — normalizar aqui
    apagaria a diferenca entre "a IA nao extraiu" e "a IA extraiu vazio"
    antes de alguem poder decidir se ela importa.

    `quantidade_dias_pretendida` e GLOBAL, no nivel da viagem, e NAO
    substitui `dias` por destino na completude (regra da Fase 2).
    """

    nome: StrictStr | None = None
    email: StrictStr | None = None
    destinos: list[DestinoTriagem] = Field(default_factory=list)
    total_pessoas: StrictInt | None = None
    adultos: StrictInt | None = None
    criancas: StrictInt | None = None
    datas_definidas: StrictBool | None = None
    quantidade_dias_pretendida: StrictInt | None = None


class AIInterpretationResult(_ContratoV2):
    """Saida da IA Interpretadora. So FATOS interpretados — nenhuma decisao.

    E o contrato que existe para impedir a regressao central deste projeto.
    Na V1, `pronto_para_humano` era produzido por uma LLM e consumido por
    outra lendo uma descricao em prosa: nunca passava por Python, e um valor
    malformado atravessava as duas camadas de tolerancia sem gerar um unico
    erro. Aqui esse campo — e qualquer equivalente operacional sob outro nome
    — e rejeitado por `extra="forbid"`, porque simplesmente nao esta
    declarado.

    `intent` e `StrictStr | None` SEM DEFAULT: a chave precisa existir, o
    valor pode ser `null`. Nao ha vocabulario aprovado (a palavra ocorre uma
    unica vez no corpus dos documentos), entao nao se inventa enum nem um
    `"unknown"` de conveniencia.

    R-INTENT: `intent` e INFORMATIVO. Nenhuma decisao de estado, fila,
    handoff, responsavel ou bot pode ramificar sobre esse texto livre
    enquanto nao existir vocabulario fechado aprovado. Ramificar sobre ele
    recria o `pronto_para_humano` com outro nome. A regra vale nas Fases 4 a
    10 e NAO depende de guarda automatica existir.

    `explicit_human_request` e obrigatorio e sem default de proposito:
    ausencia levanta, nunca vira `False` presumido. Um `False` silencioso
    significa cliente que pediu humano e nunca foi encaminhado — a falha de
    producao que este projeto existe para corrigir. Que o cliente peca humano
    e FATO interpretado; quem decide o handoff e codigo deterministico na
    Fase 6.

    `duvidas` tambem e obrigatorio e sem default: a LLM manda `[]`
    explicitamente. Default aqui tornaria indistinguivel "sem duvidas" de
    "campo esquecido na resposta".

    R-INTENT VALE PARA `duvidas` TAMBEM. Ele e o OUTRO campo de texto livre
    que sai da LLM, e o risco e identico: uma fase futura fazendo string-match
    em `duvidas` procurando "quer humano" recria o `pronto_para_humano` por
    outra porta — decisao operacional saindo de texto que ninguem validou.
    Nenhuma decisao pode ramificar sobre o CONTEUDO de `duvidas`. Contar
    quantas ha, ou repassa-las para a IA Comunicadora perguntar ao cliente,
    e uso legitimo; ler o que dizem para decidir e proibido.
    """

    intent: StrictStr | None
    explicit_human_request: StrictBool
    extracted: TriageData
    duvidas: list[StrictStr]


class ResultadoTriagem(_ContratoV2):
    """Retorno de `avaliar_triagem()` (Fase 2). Aqui so a forma.

    Sem defaults: a Fase 2 constroi os dois campos explicitamente. Um default
    em `completa` daria a uma implementacao futura a chance de devolver o
    objeto sem ter avaliado nada.
    """

    completa: StrictBool
    campos_faltantes: list[StrictStr]


class DecisaoPrefiltro(_ContratoV2):
    """Retorno de `avaliar_prefiltro()` (Fase 3). Aqui so a forma.

    `motivo` e obrigatorio-mas-anulavel: a chave sempre existe, o valor pode
    ser `null` quando nada foi ignorado. Sem enum — o vocabulario de motivos
    e decisao do subgate da Fase 3, nao invencao desta.
    """

    ignorar: StrictBool
    motivo: StrictStr | None


class ResultadoGuard(_ContratoV2):
    """Retorno de `filtrar_saida()` (Fase 9). Aqui so a forma.

    O guard em si — bloqueio deterministico de valor monetario — e da Fase 9.
    """

    permitido: StrictBool
    motivo: StrictStr | None
    texto_final: StrictStr
