"""
CONV-VAR-01 — Catalogo, parser e resolver de variaveis dinamicas (@TOKEN).

SEGURANCA — a resolucao e ESTRITAMENTE TEXTUAL:
  - nao existe eval/exec/compile/import dinamico;
  - o usuario NUNCA cadastra expressao, script ou codigo;
  - uma variavel dinamica so pode apontar para uma chave do catalogo abaixo,
    e cada chave e uma funcao Python FIXA deste modulo;
  - o valor substituido nao e re-escaneado (substituicao em passo unico), logo
    um valor que contenha "@ALGO" nao vira variavel.

CONTEXTO — cada envio monta seu proprio `VariableContext` a partir da conversa
atual. O lead do CRM e buscado por `lead_id` EXATO (nunca por aproximacao de
telefone), justamente para que o cliente A jamais receba dado do cliente B.

ESCAPE — `@@TEXTO` produz `@TEXTO` literal, processado ANTES da deteccao de
variaveis e nunca reinterpretado. E como se escreve uma arroba de verdade
(ex.: `Siga @@BRASILEIROSNOATACAMA`) sem que o sistema a trate como token.

MODOS:
  - `render(...)`           -> (texto, problemas)  — usado pelo preview
  - `render_strict(...)`    -> levanta VariableResolutionError no 1o problema
                               (envio manual: BLOQUEIA o envio)
  - `render_auto_reply(...)`-> texto so quando TUDO resolve; senao `None` e a
                               resposta automatica inteira e pulada (nunca
                               token literal, nunca texto mutilado, nunca
                               string vazia). Nao levanta excecao.
"""

import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Callable, List, NamedTuple, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.message_variable import MessageVariable

logger = logging.getLogger(__name__)

# Fuso de Brasilia (mesmo criterio ja usado em webhook.py::_is_within_business_hours)
_BRT = dt_timezone(timedelta(hours=-3))


# ─── Parser ──────────────────────────────────────────────────────────────
#
# Um token do sistema e `@` + MAIUSCULAS/digitos/underscore, comecando e
# terminando em letra/digito, e NAO precedido de caractere alfanumerico,
# ponto ou outro `@`.
#
# Os dois lookbehinds sao complementares: `[^\W_]` e "caractere de palavra
# EXCETO underscore" (alfanumerico Unicode), entao `_` e aceito ANTES do token
# — sem isso o italico do WhatsApp `_@TOKEN_` nao funcionaria. Pelo mesmo
# motivo o token nao pode TERMINAR em underscore: `_@NOME_` deve casar
# `@NOME`, e nao `@NOME_`.
#
#   contato@empresa.com     -> nao casa ('empresa' e minusculo)
#   CONTATO@EMPRESA.COM     -> nao casa (precedido por 'O', alfanumerico)
#   José@EMPRESA            -> nao casa (`[^\W_]` e Unicode-aware)
#   *@NOMEEMPRESA*          -> casa (negrito preservado)
#   _@NOMEEMPRESA_          -> casa (italico preservado)
#   @nome-com-hifen         -> nao casa (minusculo; nao e token do sistema)
#   @  /  @A                -> nao casa (minimo de 2 caracteres)
# TOKEN_PATTERN e a ESPECIFICACAO de referencia da posicao valida. A varredura
# real usa _scan() + _is_strict_position() (precisa enxergar tambem as posicoes
# recusadas, veja abaixo), que sao uma reimplementacao manual destes
# lookbehinds — `tests/test_conversas_variables.py` cruza as duas
# implementacoes para que nunca divirjam.
_TOKEN_BODY = r"[A-Z0-9][A-Z0-9_]{0,58}[A-Z0-9]"
TOKEN_PATTERN = re.compile(r"(?<![^\W_])(?<![.@])@(" + _TOKEN_BODY + r")")

# Formato completo aceito no cadastro (validacao do CRUD).
TOKEN_FULL_PATTERN = re.compile(r"^@" + _TOKEN_BODY + r"$")

# Varredura LARGA: encontra qualquer coisa com cara de token, INCLUSIVE nas
# posicoes que o padrao estrito recusa (colado a letra/digito, a um ponto ou a
# outro token). Sem ela, um token em posicao recusada seria simplesmente
# ignorado — e iria LITERALMENTE ao cliente, quebrando o invariante do modulo.
#   "@NOMECLIENTE@NOMEEMPRESA"  -> o 2o token e detectado como ambiguo
#   "Bom dia.@NOMECLIENTE"      -> detectado como ambiguo
#   "contato@empresa.com"       -> nao e candidato (minusculo)
#   "CONTATO@EMPRESA.COM"       -> candidato, mas @EMPRESA nao esta cadastrado
#                                  -> permanece LITERAL (e-mail intacto)
_LAX_TOKEN_PATTERN = re.compile(r"@[A-Z0-9][A-Z0-9_]*")

# Tokens reservados pelo sistema. Hoje o sistema nao reserva nenhum token;
# a lista existe para que uma reserva futura seja aplicada em um unico lugar.
RESERVED_TOKENS: frozenset = frozenset()


ESCAPE_SEQUENCE = "@@"


class _Piece(NamedTuple):
    """Trecho do texto que NAO e texto comum: um escape ou um candidato a token."""
    kind: str            # 'escape' | 'token'
    start: int           # indice do primeiro '@'
    end: int             # fim do trecho (no token, exclui underscores finais)
    token: Optional[str]
    strict: bool         # so para 'token': posicao valida para substituicao


def _is_strict_position(text: str, at: int) -> bool:
    """
    Equivalente aos lookbehinds de TOKEN_PATTERN: a posicao e valida quando o
    caractere anterior NAO e alfanumerico (Unicode), ponto ou `@`. Underscore
    e permitido — sem isso o italico do WhatsApp `_@TOKEN_` nao funcionaria.
    """
    if at == 0:
        return True
    prev = text[at - 1]
    if prev in ".@":
        return False
    return not prev.isalnum()


def _scan(text: Optional[str]) -> List[_Piece]:
    """
    Varredura ESQUERDA->DIREITA em passo unico: escapes e candidatos a token,
    na ordem do texto.

    O escape `@@` e reconhecido ANTES de qualquer deteccao de variavel e
    consome os dois caracteres, entao o que vem depois dele NUNCA e examinado
    como token — `@@NOMECLIENTE` sai literalmente como `@NOMECLIENTE` mesmo
    que `@NOMECLIENTE` esteja cadastrada.
    """
    if not text:
        return []
    pieces: List[_Piece] = []
    index, length = 0, len(text)
    while index < length:
        if text[index] != "@":
            index += 1
            continue
        if text.startswith(ESCAPE_SEQUENCE, index):
            pieces.append(_Piece("escape", index, index + 2, None, False))
            index += 2
            continue
        match = _LAX_TOKEN_PATTERN.match(text, index)
        if match:
            # O token nunca termina em underscore (ver TOKEN_FULL_PATTERN): em
            # "_@NOME_" o token e "@NOME" e o "_" final volta ao texto.
            token = match.group(0).rstrip("_")
            if len(token) >= 3:  # '@' + minimo de 2 caracteres
                pieces.append(_Piece(
                    kind="token",
                    start=index,
                    end=index + len(token),
                    token=token,
                    strict=_is_strict_position(text, index),
                ))
                index += len(token)
                continue
        index += 1
    return pieces


def find_tokens(text: Optional[str]) -> List[str]:
    """Tokens em posicao VALIDA (escapes excluidos), unicos e na ordem."""
    seen: List[str] = []
    for piece in _scan(text):
        if piece.kind == "token" and piece.strict and piece.token not in seen:
            seen.append(piece.token)
    return seen


def count_token_references(text: Optional[str], token: str) -> int:
    """
    Quantas vezes `token` aparece em `text` COMO variavel que seria SUBSTITUIDA.

    Comparacao exata (token inteiro, nunca `startswith`) e restrita as
    ocorrencias ESTRITAS — as mesmas que `render` substituiria. Ficam de fora,
    por definicao:
      - arroba escapada  `@@TOKEN`  (o scanner nem gera candidato);
      - e-mail e mencao  `a@TOKEN.com`, `100@TOKEN` (posicao recusada).

    Ocorrencia ambigua (`dia.@TOKEN`) tambem NAO conta: hoje ela bloqueia o
    envio justamente por ser suspeita, e excluir a variavel apenas a devolve
    ao texto como literal — nao quebra mensagem nenhuma.
    """
    return sum(
        1 for piece in _scan(text)
        if piece.kind == "token" and piece.strict and piece.token == token
    )


def normalize_token(raw: Optional[str]) -> str:
    """Normaliza o token digitado: trim, MAIUSCULAS e prefixo `@` garantido."""
    token = (raw or "").strip().upper()
    if token and not token.startswith("@"):
        token = "@" + token
    return token


# ─── Contexto de resolucao ───────────────────────────────────────────────

class VariableContext:
    """
    Dados da conversa ATUAL. Um contexto por envio — nada e reaproveitado
    entre conversas (sem cache global, sem estado de modulo).
    """

    def __init__(self, db: Session, conversation=None, user=None):
        self.db = db
        self.conversation = conversation
        self.user = user
        self._lead_loaded = False
        self._lead = None

    def lead(self) -> Optional[dict]:
        """
        Lead do CRM por `lead_id` EXATO (tabela compartilhada `leads`).

        Deliberadamente NAO usa `crm.lookup_lead_by_whatsapp`, que casa por
        `LIKE '%ultimos 10 digitos%'` e poderia devolver o lead de OUTRO
        cliente. Cache por contexto (1 query por envio), nunca entre envios.
        """
        if self._lead_loaded:
            return self._lead
        self._lead_loaded = True
        conv = self.conversation
        lead_id = getattr(conv, "lead_id", None) if conv is not None else None
        if not lead_id or lead_id <= 0:
            return None
        try:
            row = self.db.execute(
                sql_text("SELECT id, nome, email FROM leads WHERE id = :lid"),
                {"lid": lead_id},
            ).fetchone()
            if row is not None:
                self._lead = {"id": row.id, "nome": row.nome, "email": row.email}
        except Exception as exc:  # tabela ausente em teste/dev isolado
            # Rollback OBRIGATORIO (mesmo padrao de services/crm.py): no
            # PostgreSQL a query falha deixa a transacao ABORTADA e todo
            # comando seguinte falharia — inclusive o commit de
            # record_outbound_message, DEPOIS do envio ja aceito pela Meta.
            # Sem isto, a mensagem chegaria ao cliente sem ser persistida.
            db_rollback_failed = False
            try:
                self.db.rollback()
            except Exception:
                db_rollback_failed = True
            # Resumo SEGURO: apenas a classe do erro — o texto da excecao do
            # SQLAlchemy pode carregar SQL e parametros vinculados.
            logger.warning(
                f"Nao foi possivel carregar o lead {lead_id} do CRM "
                f"({type(exc).__name__}; rollback_ok={not db_rollback_failed})"
            )
        return self._lead


# ─── Helpers de valor ────────────────────────────────────────────────────

def first_name(full_name: Optional[str]) -> Optional[str]:
    """
    Primeiro nome: trim, separa por espacos, primeiro segmento nao vazio.
    Preserva Unicode/acentos ("  Ana Maria" -> "Ana", "Érica Souza" -> "Érica").
    """
    parts = (full_name or "").split()
    return parts[0] if parts else None


def _format_phone(raw: Optional[str]) -> Optional[str]:
    """Formata o telefone como a UI ja faz (conversas.js::formatPhone)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 13:
        return f"+{digits[0:2]} ({digits[2:4]}) {digits[4:9]}-{digits[9:]}"
    return raw


def _now_brt() -> datetime:
    return datetime.now(dt_timezone.utc).astimezone(_BRT)


# ─── Catalogo de propriedades dinamicas ──────────────────────────────────
#
# Somente propriedades que EXISTEM de fato no sistema. Nome e e-mail da
# empresa NAO possuem tabela/configuracao no Conversas — devem ser cadastrados
# como variaveis do tipo FIXO (ex.: @NOMEEMPRESA = "Brasileiros no Atacama").

def _src_empresa_expediente(ctx: VariableContext) -> Optional[str]:
    from app.models.auto_reply import BusinessHours

    weekday = _now_brt().weekday()
    hours = ctx.db.query(BusinessHours).filter(BusinessHours.weekday == weekday).first()
    if hours is None:
        return None
    if not hours.is_open:
        return "fechado hoje"
    if hours.open_time and hours.close_time:
        return f"{hours.open_time} às {hours.close_time}"
    return None


def _src_atendente_nome(ctx: VariableContext) -> Optional[str]:
    return getattr(ctx.user, "nome", None) if ctx.user is not None else None


def _src_atendente_email(ctx: VariableContext) -> Optional[str]:
    return getattr(ctx.user, "email", None) if ctx.user is not None else None


def _src_responsavel_nome(ctx: VariableContext) -> Optional[str]:
    return getattr(ctx.conversation, "responsavel_nome", None) if ctx.conversation is not None else None


def _cliente_nome_completo(ctx: VariableContext) -> Optional[str]:
    conv = ctx.conversation
    nome = (getattr(conv, "nome", None) or "").strip() if conv is not None else ""
    if nome:
        # Quando o contato e desconhecido, a conversa guarda o proprio numero
        # no campo `nome` (ver conversations.py::initiate) — isso NAO e um nome.
        looks_like_phone = re.sub(r"[\s+()\-]", "", nome).isdigit()
        if not looks_like_phone:
            return nome
    lead = ctx.lead()
    lead_nome = (lead or {}).get("nome")
    return lead_nome.strip() if lead_nome and lead_nome.strip() else None


def _src_cliente_primeiro_nome(ctx: VariableContext) -> Optional[str]:
    return first_name(_cliente_nome_completo(ctx))


def _src_cliente_telefone(ctx: VariableContext) -> Optional[str]:
    return _format_phone(getattr(ctx.conversation, "whatsapp", None) if ctx.conversation is not None else None)


def _src_cliente_email(ctx: VariableContext) -> Optional[str]:
    lead = ctx.lead()
    email = (lead or {}).get("email")
    return email.strip() if email and email.strip() else None


def _src_conversa_numero(ctx: VariableContext) -> Optional[str]:
    conv_id = getattr(ctx.conversation, "id", None) if ctx.conversation is not None else None
    return str(conv_id) if conv_id else None


def _src_conversa_data(ctx: VariableContext) -> Optional[str]:
    created = getattr(ctx.conversation, "created_at", None) if ctx.conversation is not None else None
    if created is None:
        return None
    if created.tzinfo is not None:
        created = created.astimezone(_BRT)
    return created.strftime("%d/%m/%Y")


def _src_data_hoje(ctx: VariableContext) -> Optional[str]:
    return _now_brt().strftime("%d/%m/%Y")


class SourceProperty(NamedTuple):
    key: str
    group: str
    label: str
    resolve: Callable[[VariableContext], Optional[str]]


# Ordem preservada: e a ordem exibida no seletor da interface.
CATALOG: "dict[str, SourceProperty]" = {
    p.key: p
    for p in (
        SourceProperty("empresa.expediente_hoje", "Empresa", "Empresa: Expediente do Dia", _src_empresa_expediente),
        SourceProperty("atendente.nome", "Funcionário", "Funcionário: Nome", _src_atendente_nome),
        SourceProperty("atendente.email", "Funcionário", "Funcionário: Email", _src_atendente_email),
        SourceProperty("conversa.responsavel_nome", "Funcionário", "Funcionário: Responsável pela Conversa", _src_responsavel_nome),
        SourceProperty("cliente.nome_completo", "Cliente", "Cliente: Nome Completo", _cliente_nome_completo),
        SourceProperty("cliente.primeiro_nome", "Cliente", "Cliente: Primeiro Nome", _src_cliente_primeiro_nome),
        SourceProperty("cliente.telefone", "Cliente", "Cliente: Telefone", _src_cliente_telefone),
        SourceProperty("cliente.email", "Cliente", "Cliente: Email", _src_cliente_email),
        SourceProperty("conversa.numero", "Conversa", "Conversa: Número (protocolo)", _src_conversa_numero),
        SourceProperty("conversa.data", "Conversa", "Conversa: Data de início", _src_conversa_data),
        SourceProperty("data.hoje", "Data", "Data: Data Atual", _src_data_hoje),
    )
}


def catalog_groups() -> List[dict]:
    """Catalogo agrupado, no formato consumido pelo seletor da interface."""
    groups: List[dict] = []
    index: "dict[str, dict]" = {}
    for prop in CATALOG.values():
        if prop.group not in index:
            index[prop.group] = {"group": prop.group, "options": []}
            groups.append(index[prop.group])
        index[prop.group]["options"].append({"key": prop.key, "label": prop.label})
    return groups


# ─── Resolucao ───────────────────────────────────────────────────────────

class VariableProblem(NamedTuple):
    token: str
    code: str      # unknown | inactive | empty_fixed | empty_dynamic | invalid_source
    short: str     # rotulo curto (preview)
    message: str   # mensagem completa para o vendedor (envio bloqueado)


class VariableResolutionError(Exception):
    """Envio bloqueado: ha token que nao pode ser resolvido com seguranca."""

    def __init__(self, problem: VariableProblem):
        self.problem = problem
        self.token = problem.token
        super().__init__(problem.message)


_BLOCK_PREFIX = "Não foi possível enviar a mensagem."


def _problem(token: str, code: str) -> VariableProblem:
    if code == "unknown":
        return VariableProblem(
            token, code, "não é uma variável cadastrada",
            f"{_BLOCK_PREFIX} {token} não é uma variável cadastrada.",
        )
    if code == "inactive":
        return VariableProblem(
            token, code, "variável desativada",
            f"{_BLOCK_PREFIX} A variável {token} está desativada.",
        )
    if code == "empty_fixed":
        return VariableProblem(
            token, code, "sem valor configurado",
            f"{_BLOCK_PREFIX} A variável {token} não possui um valor configurado.",
        )
    if code == "invalid_source":
        return VariableProblem(
            token, code, "origem inválida",
            f"{_BLOCK_PREFIX} A variável {token} está ligada a uma propriedade que não existe mais.",
        )
    if code == "ambiguous":
        return VariableProblem(
            token, code, "colada a outro texto",
            f"{_BLOCK_PREFIX} A variável {token} está colada a outro texto. "
            f"Deixe um espaço antes dela.",
        )
    return VariableProblem(
        token, code, "sem valor para este contato",
        f"{_BLOCK_PREFIX} A variável {token} não possui valor para este contato.",
    )


def render(db: Session, text: Optional[str], ctx: VariableContext):
    """
    Aplica o escape `@@` e substitui os tokens CADASTRADOS pelo valor do
    contexto. Retorna `(texto_renderizado, problemas)` — nao levanta excecao.

    Tokens com problema sao substituidos por string vazia, mas NENHUM chamador
    envia um texto com problemas: `render_strict` bloqueia e
    `render_auto_reply` pula a resposta inteira. Um token NUNCA vai
    literalmente para o cliente.
    """
    pieces = _scan(text)
    if not pieces:
        return text or "", []

    candidates = [p for p in pieces if p.kind == "token"]
    registered = {}
    if candidates:
        rows = db.query(MessageVariable).filter(
            MessageVariable.token.in_({c.token for c in candidates})
        ).all()
        registered = {row.token: row for row in rows}

    values: "dict[str, str]" = {}
    problems: List[VariableProblem] = []

    tokens: List[str] = []
    for candidate in candidates:
        if candidate.strict and candidate.token not in tokens:
            tokens.append(candidate.token)

    for token in tokens:
        variable = registered.get(token)
        if variable is None:
            problems.append(_problem(token, "unknown"))
            values[token] = ""
            continue
        if not variable.is_active:
            problems.append(_problem(token, "inactive"))
            values[token] = ""
            continue

        if variable.kind == "fixed":
            value = (variable.fixed_value or "").strip()
            if not value:
                problems.append(_problem(token, "empty_fixed"))
                values[token] = ""
            else:
                values[token] = value
            continue

        prop = CATALOG.get(variable.source_key or "")
        if prop is None:
            problems.append(_problem(token, "invalid_source"))
            values[token] = ""
            continue

        try:
            resolved = prop.resolve(ctx)
        except Exception as exc:  # nunca derruba o envio por erro de origem
            # Resumo SEGURO: token + origem + classe do erro (nunca o valor
            # resolvido nem o texto bruto da excecao — podem conter dado do
            # cliente). O envio e bloqueado adiante como "sem valor".
            logger.warning(
                f"Falha ao resolver {token} (origem={variable.source_key}, "
                f"erro={type(exc).__name__})"
            )
            resolved = None

        resolved = "" if resolved is None else str(resolved).strip()
        if not resolved:
            problems.append(_problem(token, "empty_dynamic"))
            values[token] = ""
        else:
            values[token] = resolved

    # Ocorrencia CADASTRADA em posicao recusada (colada a outro texto ou a
    # outro token): e claramente um token do sistema, entao vira problema e
    # sai do texto. Ocorrencia NAO cadastrada em posicao recusada permanece
    # LITERAL — e o que mantem "contato@empresa.com" intacto.
    for candidate in candidates:
        if candidate.strict or candidate.token not in registered:
            continue
        if not any(p.token == candidate.token and p.code == "ambiguous" for p in problems):
            problems.append(_problem(candidate.token, "ambiguous"))

    # Passo unico sobre o texto ORIGINAL: o valor substituido NAO e
    # re-escaneado (sem recursao) e backreferences de regex (`\1`) entram
    # como texto literal (montagem manual, sem re.sub). Formatacao do
    # WhatsApp, emojis e quebras de linha atravessam intactos porque tudo
    # fora dos trechos identificados e copiado byte a byte.
    out: List[str] = []
    cursor = 0
    for piece in pieces:
        out.append(text[cursor:piece.start])
        if piece.kind == "escape":
            out.append("@")                       # '@@' -> exatamente um '@'
        elif piece.strict:
            out.append(values.get(piece.token, ""))
        elif piece.token in registered:
            out.append("")                        # ambiguo: problema ja registrado
        else:
            out.append(text[piece.start:piece.end])  # e-mail/mencao: literal
        cursor = piece.end

    out.append(text[cursor:])
    return "".join(out), problems


def _ctx_conversation_id(ctx: VariableContext):
    """Id da conversa para log, quando disponivel (webhook pode nao ter)."""
    return getattr(ctx.conversation, "id", None) if ctx.conversation is not None else None


def render_strict(db: Session, text: Optional[str], ctx: VariableContext) -> str:
    """Envio manual: qualquer token nao resolvido BLOQUEIA o envio."""
    rendered, problems = render(db, text, ctx)
    if problems:
        raise VariableResolutionError(problems[0])
    return rendered


def render_auto_reply(
    db: Session,
    text: Optional[str],
    ctx: VariableContext,
    *,
    trigger: str,
) -> Optional[str]:
    """
    Respostas automaticas — TUDO OU NADA.

    Devolve o texto renderizado APENAS quando todas as variaveis resolvem.
    Em qualquer problema (desconhecida, inativa, sem valor fixo, sem valor
    dinamico, origem invalida, token colado a outro texto) devolve `None`, e
    o chamador PULA aquela resposta automatica especifica.

    Isso substitui o antigo modo tolerante, que removia o token e podia
    entregar texto mutilado ("Somos a , prazer."). As garantias agora sao:
    nunca token literal, nunca texto mutilado, nunca string vazia.

    Nunca levanta excecao: uma resposta automatica com defeito nao pode
    interromper o processamento do webhook. Falha inesperada tambem devolve
    `None`.
    """
    try:
        rendered, problems = render(db, text, ctx)
    except Exception as exc:
        logger.warning(
            "auto_reply_skipped trigger=%s conversation_id=%s motivo=erro_inesperado erro=%s",
            trigger, _ctx_conversation_id(ctx), type(exc).__name__,
        )
        return None

    if problems:
        # Log ESTRUTURADO e seguro: tipo da resposta, token, codigo do
        # problema e conversa. NUNCA o valor resolvido (pode ser dado do
        # cliente) nem o texto final.
        for problem in problems:
            logger.warning(
                "auto_reply_skipped trigger=%s conversation_id=%s token=%s problema=%s",
                trigger, _ctx_conversation_id(ctx), problem.token, problem.code,
            )
        return None

    rendered = (rendered or "").strip()
    if not rendered:
        logger.warning(
            "auto_reply_skipped trigger=%s conversation_id=%s motivo=texto_vazio",
            trigger, _ctx_conversation_id(ctx),
        )
        return None
    return rendered
