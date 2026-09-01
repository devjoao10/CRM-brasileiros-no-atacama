"""
BIA-V2 Fase 1 - contratos Pydantic puros de conversas/app/v2/contratos.py.

Cobre SO FORMA: os 6 modelos (`DestinoTriagem`, `TriageData`,
`AIInterpretationResult`, `ResultadoTriagem`, `DecisaoPrefiltro`,
`ResultadoGuard`), o `extra="forbid"` herdado de `_ContratoV2` em cada um,
os tipos estritos (`StrictStr`/`StrictInt`/`StrictBool`), e a inercia de
import do modulo (zero SQLAlchemy/app.database/httpx/app.v2.eventos).
Regra de negocio - completude de triagem, vocabulario do pre-filtro,
maquina de estados, handoff, guard de saida - e das Fases 2+ e NAO e
testada aqui. Ver a docstring de conversas/app/v2/contratos.py.

O check mais importante deste arquivo e o 6: `AIInterpretationResult`
rejeita `pronto_para_humano`. Esse campo, produzido por uma LLM e
consumido por outra via prosa sem nunca passar por Python, foi a causa
raiz da falha de producao que esta V2 existe para corrigir.

Roda standalone:  python tests/test_v2_contratos.py
"""
import ast
import pathlib
import subprocess
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from pydantic import BaseModel, ValidationError  # noqa: E402

from app.v2.contratos import (  # noqa: E402
    AIInterpretationResult,
    DecisaoPrefiltro,
    DestinoTriagem,
    ResultadoGuard,
    ResultadoTriagem,
    TriageData,
)
import app.v2.contratos as _contratos_mod  # noqa: E402


def levanta(fn, label):
    """Chama fn() e confirma que levanta ValidationError - nunca Exception generica.

    Falha (sem mascarar) se fn() nao levantar nada, ou se levantar um tipo
    diferente de ValidationError.
    """
    try:
        fn()
    except ValidationError:
        check(True, label)
    except Exception as exc:  # noqa: BLE001 - queremos relatar o tipo errado, nao mascarar
        check(False, f"{label} (levantou {type(exc).__name__}, nao ValidationError: {exc!r})")
    else:
        check(False, f"{label} (nao levantou)")


MODELOS = {
    "DestinoTriagem": DestinoTriagem,
    "TriageData": TriageData,
    "AIInterpretationResult": AIInterpretationResult,
    "ResultadoTriagem": ResultadoTriagem,
    "DecisaoPrefiltro": DecisaoPrefiltro,
    "ResultadoGuard": ResultadoGuard,
}


def _payload_valido(nome):
    """Kwargs minimos validos para construir cada um dos 6 modelos."""
    if nome == "DestinoTriagem":
        return {"destino": "Atacama"}
    if nome == "TriageData":
        return {}
    if nome == "AIInterpretationResult":
        return {
            "intent": None,
            "explicit_human_request": False,
            "extracted": TriageData(),
            "duvidas": [],
        }
    if nome == "ResultadoTriagem":
        return {"completa": False, "campos_faltantes": ["nome"]}
    if nome == "DecisaoPrefiltro":
        return {"ignorar": False, "motivo": None}
    if nome == "ResultadoGuard":
        return {"permitido": True, "motivo": None, "texto_final": "ok"}
    raise AssertionError(f"modelo desconhecido: {nome}")


# Fonte do modulo, lida uma unica vez - reusada pelos checks 21a (AST dos
# imports diretos) e 24a (ausencia de validator de negocio no texto, reforcada
# por introspecao real em 24b).
_CODIGO_FONTE = (CONVERSAS_DIR / "app" / "v2" / "contratos.py").read_text(encoding="utf-8")


# --- 1. Todos os 6 modelos importam e sao BaseModel ------------------------
for _nome, _cls in MODELOS.items():
    check(isinstance(_cls, type) and issubclass(_cls, BaseModel), f"1. {_nome} importa e e um BaseModel")


# --- 2. Payload minimo valido constroi cada um dos 6 ------------------------
for _nome, _cls in MODELOS.items():
    try:
        _cls(**_payload_valido(_nome))
        _ok, _erro = True, None
    except ValidationError as exc:
        _ok, _erro = False, exc
    check(_ok, f"2. {_nome} constroi com payload minimo valido (erro={_erro!r})")


# --- 3/4. extra="forbid" e EFETIVO em cada um dos 6, testado individualmente
# (nao presumido por heranca de _ContratoV2) - um campo desconhecido levanta
# ValidationError em todos.
for _nome, _cls in MODELOS.items():
    _kwargs = {**_payload_valido(_nome), "campo_desconhecido_qualquer": "x"}
    levanta(
        lambda _cls=_cls, _kwargs=_kwargs: _cls(**_kwargs),
        f"3/4. {_nome} rejeita campo extra (extra='forbid')",
    )


# --- 5. Cada campo obrigatorio ausente levanta ValidationError -------------
# TriageData fica de fora: TODO campo dela e opcional (ver check 10).
_CAMPOS_OBRIGATORIOS = {
    "DestinoTriagem": ["destino"],
    "AIInterpretationResult": ["intent", "explicit_human_request", "extracted", "duvidas"],
    "ResultadoTriagem": ["completa", "campos_faltantes"],
    "DecisaoPrefiltro": ["ignorar", "motivo"],
    "ResultadoGuard": ["permitido", "motivo", "texto_final"],
}
for _nome, _campos in _CAMPOS_OBRIGATORIOS.items():
    _cls = MODELOS[_nome]
    for _campo in _campos:
        _kwargs = _payload_valido(_nome)
        _kwargs.pop(_campo)
        levanta(
            lambda _cls=_cls, _kwargs=_kwargs: _cls(**_kwargs),
            f"5. {_nome} sem '{_campo}' levanta ValidationError",
        )


# ============================================================================
# 6. *** REGRESSAO CENTRAL DESTA ARQUITETURA ***
# Na V1, `pronto_para_humano` era produzido por uma LLM e consumido por outra
# lendo prosa - nunca passava por Python, e um valor malformado atravessava
# as duas camadas sem gerar erro, deixando cliente sem atendimento. Este
# contrato existe para que isso seja estruturalmente impossivel: o campo nao
# esta declarado, entao `extra="forbid"` rejeita.
# ============================================================================
levanta(
    lambda: AIInterpretationResult(
        intent=None,
        explicit_human_request=False,
        extracted=TriageData(),
        duvidas=[],
        pronto_para_humano=True,
    ),
    "6. *** REGRESSAO CENTRAL *** AIInterpretationResult com 'pronto_para_humano' levanta ValidationError",
)


# --- 7. explicit_human_request ausente levanta -----------------------------
levanta(
    lambda: AIInterpretationResult(intent=None, extracted=TriageData(), duvidas=[]),
    "7. AIInterpretationResult sem explicit_human_request levanta ValidationError (sem default presumido)",
)

# --- 8. explicit_human_request=False explicito e aceito --------------------
try:
    _air_false = AIInterpretationResult(
        intent=None, explicit_human_request=False, extracted=TriageData(), duvidas=[],
    )
    check(_air_false.explicit_human_request is False, "8. explicit_human_request=False explicito e aceito e preservado")
except ValidationError as exc:
    check(False, f"8. explicit_human_request=False explicito e aceito (erro={exc!r})")


# --- 9. intent: string valida / None explicito / chave ausente -------------
_air_intent_str = AIInterpretationResult(
    intent="quer viajar para o Atacama", explicit_human_request=False, extracted=TriageData(), duvidas=[],
)
check(_air_intent_str.intent == "quer viajar para o Atacama", "9a. intent aceita string valida")

_air_intent_none = AIInterpretationResult(
    intent=None, explicit_human_request=False, extracted=TriageData(), duvidas=[],
)
check(_air_intent_none.intent is None, "9b. intent aceita None explicito")

levanta(
    lambda: AIInterpretationResult(explicit_human_request=False, extracted=TriageData(), duvidas=[]),
    "9c. intent com CHAVE AUSENTE levanta ValidationError (sem default - a chave precisa existir)",
)


# --- 10. TriageData() sem argumentos constroi; tudo None exceto destinos --
_td_vazio = TriageData()
check(_td_vazio.destinos == [], "10a. TriageData() constroi e destinos == []")
_outros_campos = {k: v for k, v in _td_vazio.model_dump().items() if k != "destinos"}
check(
    all(v is None for v in _outros_campos.values()),
    f"10b. TriageData() todos os outros campos sao None ({_outros_campos})",
)


# --- 11. duas TriageData() NAO compartilham a lista destinos ---------------
_td_a = TriageData()
_td_b = TriageData()
_td_a.destinos.append(DestinoTriagem(destino="Atacama"))
check(
    _td_b.destinos == [] and _td_a.destinos is not _td_b.destinos,
    f"11. mutar destinos de uma TriageData() nao afeta outra (default_factory) - b.destinos={_td_b.destinos!r}",
)


# --- 12. DestinoTriagem(destino=...) valido, datas e dias todos None -------
_dest_minimo = DestinoTriagem(destino="Atacama")
check(
    _dest_minimo.data_inicio is None and _dest_minimo.data_fim is None and _dest_minimo.dias is None,
    "12. DestinoTriagem(destino='Atacama') valido com data_inicio/data_fim/dias todos None",
)


# --- 13. TriageData incompleta e estruturalmente valida --------------------
try:
    TriageData(nome="Joao")
    _ok13 = True
except ValidationError:
    _ok13 = False
check(_ok13, "13. TriageData incompleta (so 'nome') e estruturalmente valida - completude e da Fase 2")


# --- 14. TriageData com destinos MISTOS (datas + dias) e representavel -----
# Nada neste modulo declara completude ou proibe modo misto - isso e
# `avaliar_triagem()` na Fase 2 (ver check 24 para a ausencia de validator).
try:
    _td_mista = TriageData(destinos=[
        DestinoTriagem(destino="Atacama", data_inicio=date(2026, 1, 10), data_fim=date(2026, 1, 20)),
        DestinoTriagem(destino="Uyuni", dias=3),
    ])
    _ok14 = len(_td_mista.destinos) == 2
except ValidationError:
    _ok14 = False
check(
    _ok14,
    "14. TriageData com um destino de DATAS e outro de DIAS na mesma lista constroi - "
    "nada aqui declara completude ou proibe modo misto (decisao da Fase 2)",
)


# --- 15. StrictInt rejeita True e False em todo campo numerico -------------
_CAMPOS_STRICT_INT = [
    ("DestinoTriagem", {"destino": "x"}, "dias"),
    ("TriageData", {}, "total_pessoas"),
    ("TriageData", {}, "adultos"),
    ("TriageData", {}, "criancas"),
    ("TriageData", {}, "quantidade_dias_pretendida"),
]
for _nome, _base, _campo in _CAMPOS_STRICT_INT:
    _cls = MODELOS[_nome]
    for _valor_bool in (True, False):
        _kwargs = {**_base, _campo: _valor_bool}
        levanta(
            lambda _cls=_cls, _kwargs=_kwargs: _cls(**_kwargs),
            f"15. {_nome}.{_campo} rejeita bool {_valor_bool!r} (StrictInt)",
        )


# --- 16. StrictBool rejeita 0, 1, "true", "false" em todo campo booleano ---
_CAMPOS_STRICT_BOOL = [
    ("TriageData", {}, "datas_definidas"),
    ("AIInterpretationResult", {"intent": None, "extracted": TriageData(), "duvidas": []}, "explicit_human_request"),
    ("ResultadoTriagem", {"campos_faltantes": []}, "completa"),
    ("DecisaoPrefiltro", {"motivo": None}, "ignorar"),
    ("ResultadoGuard", {"motivo": None, "texto_final": "ok"}, "permitido"),
]
for _nome, _base, _campo in _CAMPOS_STRICT_BOOL:
    _cls = MODELOS[_nome]
    for _valor_ruim in (0, 1, "true", "false"):
        _kwargs = {**_base, _campo: _valor_ruim}
        levanta(
            lambda _cls=_cls, _kwargs=_kwargs: _cls(**_kwargs),
            f"16. {_nome}.{_campo} rejeita {_valor_ruim!r} (StrictBool)",
        )


# --- 17. date: objeto real / string ISO / roundtrip JSON / valor invalido --
_dest_date_obj = DestinoTriagem(destino="x", data_inicio=date(2026, 1, 15))
check(_dest_date_obj.data_inicio == date(2026, 1, 15), "17a. DestinoTriagem aceita objeto date() real")

_dest_date_str = DestinoTriagem(destino="x", data_inicio="2026-01-15")
check(_dest_date_str.data_inicio == date(2026, 1, 15), "17b. DestinoTriagem aceita string ISO e converte para date")

_dest_json = _dest_date_str.model_dump_json()
_dest_reload = DestinoTriagem.model_validate_json(_dest_json)
check(
    _dest_reload == _dest_date_str,
    f"17c. roundtrip model_dump_json()->model_validate_json() preserva a data (json={_dest_json!r})",
)

levanta(
    lambda: DestinoTriagem(destino="x", data_inicio="nao-e-uma-data"),
    "17d. string obviamente invalida em data_inicio levanta ValidationError",
)


# --- 18. email e string plana; SEM dependencia de EmailStr -----------------
# `email-validator` NAO esta em conversas/requirements.txt. Esta SIM no
# requirements.txt da raiz (linha 21), que e do CRM — e os dois apps rodam em
# ambientes SEPARADOS de proposito (os pins conflitam; ver CLAUDE.md). Entao
# numa maquina onde alguem instalou o ambiente do CRM a lib esta presente por
# motivo legitimo, e `EmailStr` funcionaria aqui — mas quebraria com
# ImportError, na definicao da classe, no venv do Conversas e no CI. E o tipo
# de dependencia que passa no laptop e falha no deploy; dai o cuidado.
#
# Tres propriedades garantem isso, e NENHUMA depende do ambiente:
#   (a) o tipo REAL resolvido do campo `email` e string simples (aqui, 18b);
#   (b) `import email_validator` DIRETO seria pego pelo check 21a (AST), que
#       so admite as raizes `datetime` e `pydantic`;
#   (c) `EmailStr` em QUALQUER campo — inclusive fora de `email`, onde 18b nao
#       olha — e pego pelo check 21c, porque `email_validator` esta em
#       `_SUSPEITOS_IMPORT`.
#
# (c) NAO e redundante com (b), e a distincao importa: `21a` PERMITE a raiz
# `pydantic`, entao `from pydantic import EmailStr` passa por ele sem alarme.
# O que dispara a dependencia nao e importar o nome, e USA-LO como tipo de
# campo — ai o Pydantic chama `import_email_validator()` ao montar o schema,
# na definicao da classe. Quem detecta esse caso e so o delta de runtime do
# 21c. Se alguem concluir que a entrada `email_validator` em `_SUSPEITOS_IMPORT`
# e redundante e remove-la, a lacuna reabre.
#
# Versoes anteriores deste bloco tinham mais tres checks, todos removidos por
# medirem a coisa errada:
#   - dois por TEXTO (`"EmailStr" not in _CODIGO_FONTE`): bastaria alguem
#     escrever "decidimos nao usar EmailStr" num comentario para quebrar o
#     teste sem nenhuma mudanca semantica - a mesma armadilha do check 22,
#     onde "DomainEvent" aparece na docstring;
#   - um por PRESENCA ABSOLUTA (`"email_validator" not in sys.modules`): se a
#     lib vier pre-carregada por sitecustomize, plugin ou outra dependencia, o
#     teste falha mesmo que contratos.py nunca a importe. E exatamente o
#     defeito que o check 21 corrigiu, repetido aqui.
_td_email = TriageData(email="nao-e-um-email-formatado-de-verdade")
check(
    _td_email.email == "nao-e-um-email-formatado-de-verdade",
    "18a. TriageData.email aceita qualquer string, sem validar formato de e-mail",
)
_anot_email = repr(TriageData.model_fields["email"].annotation)
check(
    "EmailStr" not in _anot_email and "str" in _anot_email,
    f"18b. tipo resolvido de TriageData.email e str simples, nao EmailStr "
    f"(real={_anot_email})",
)


# --- 19. campos operacionais sao rejeitados em AIInterpretationResult ------
# Mecanismo e estrutural (extra="forbid", allowlist), nao uma denylist destes
# nomes especificos - eles sao so os candidatos mais plausiveis a recriar o
# `pronto_para_humano` (estado, fila, responsavel, bot, funil).
_CAMPOS_OPERACIONAIS = [
    "state", "state_version", "handoff", "handoff_completed", "attendant_id",
    "responsavel_id", "responsible_id", "bot_active", "is_bot_active",
    "funnel", "funil", "stage", "etapa", "status_venda",
]
_base_air = _payload_valido("AIInterpretationResult")
for _campo in _CAMPOS_OPERACIONAIS:
    _kwargs = {**_base_air, _campo: "qualquer-valor"}
    levanta(
        lambda _kwargs=_kwargs: AIInterpretationResult(**_kwargs),
        f"19. AIInterpretationResult rejeita campo operacional '{_campo}'",
    )


# --- 20. model_dump() -> model_validate() preserva o contrato -------------
_INSTANCIAS_RICAS = {
    "DestinoTriagem": DestinoTriagem(
        destino="Atacama", data_inicio=date(2026, 1, 1), data_fim=date(2026, 1, 10), dias=None,
    ),
    "TriageData": TriageData(
        nome="Joao", email="joao@example.com",
        destinos=[DestinoTriagem(destino="Atacama", dias=5)],
        total_pessoas=2, adultos=2, criancas=0, datas_definidas=True,
        quantidade_dias_pretendida=5,
    ),
    "AIInterpretationResult": AIInterpretationResult(
        intent="quer viajar para o Atacama",
        explicit_human_request=True,
        extracted=TriageData(nome="Joao"),
        duvidas=["quantas pessoas?"],
    ),
    "ResultadoTriagem": ResultadoTriagem(completa=False, campos_faltantes=["email", "destinos"]),
    "DecisaoPrefiltro": DecisaoPrefiltro(ignorar=True, motivo="fora do horario comercial"),
    "ResultadoGuard": ResultadoGuard(permitido=False, motivo="valor monetario", texto_final="texto seguro"),
}
for _nome, _instancia in _INSTANCIAS_RICAS.items():
    _cls = MODELOS[_nome]
    _dump = _instancia.model_dump()
    _recarregado = _cls.model_validate(_dump)
    check(_recarregado == _instancia, f"20. {_nome}: model_dump() -> model_validate() preserva o contrato")


# --- 21. inercia de import: DUAS provas complementares ---------------------
# A versao anterior deste check media o ESTADO ABSOLUTO de `sys.modules` depois
# de importar `contratos.py`, e exigia ausencia total dos suspeitos. Isso
# confundia duas propriedades diferentes:
#
#   (A) acoplamento introduzido por `contratos.py`;
#   (B) o que o proprio Pydantic / sitecustomize / ambiente ja carregam.
#
# Num ambiente onde importar `pydantic` ja traz `requests` para `sys.modules`,
# o check acusava falha sem que `contratos.py` tivesse relacao nenhuma com
# `requests`. Falso positivo: `requests in sys.modules` NAO prova acoplamento.
# E teste que passa numa maquina e falha noutra por motivo alheio ao codigo
# acaba desativado, que e o pior desfecho para uma guarda.
#
# As duas provas abaixo separam (A) de (B).
_SUSPEITOS_IMPORT = [
    "sqlalchemy",
    "app.database",
    "app.models",
    "httpx",
    "requests",
    "fastapi",
    "app.v2.eventos",
    # `email_validator` entra aqui pela lacuna que a revisao do check 18 expos:
    # 18a/18b so inspecionam `TriageData.email`, e `21a` deixa passar
    # `from pydantic import EmailStr` porque `pydantic` e raiz permitida. Sem
    # esta linha, usar EmailStr em QUALQUER outro campo nao seria detectado —
    # e o check 18e removido, apesar de fragil, cobria o modulo inteiro.
    "email_validator",
]

# PROVA A - imports DIRETOS, por AST. Nao depende de runtime nem de ambiente:
# le a arvore sintatica do arquivo e exige que TODO import venha de
# `datetime` ou `pydantic`. `ast.walk` percorre a arvore INTEIRA, entao pega
# tambem import escondido dentro de funcao, `if` ou `try` — nao so os do topo.
# A raiz vem de `alias.name`, nao de `asname`: `import requests as rq` nao
# escapa. Pega mesmo que o modulo nunca seja executado.
#
# O que a PROVA A NAO pega: import dinamico (`__import__("requests")` e um
# Call, nao um Import). Limitacao inerente a analise estatica; a PROVA B cobre
# em runtime quando o alvo e um dos suspeitos.
_MODULOS_RAIZ_PERMITIDOS = {"datetime", "pydantic"}


def _raizes_importadas(codigo_fonte: str) -> set[str]:
    """Modulos-raiz de todo import direto do arquivo (via AST)."""
    raizes = set()
    for no in ast.walk(ast.parse(codigo_fonte)):
        if isinstance(no, ast.Import):
            for alias in no.names:
                raizes.add(alias.name.split(".")[0])
        elif isinstance(no, ast.ImportFrom):
            # `from . import x` tem module=None; guarda o ponto como raiz
            # relativa, que tambem nao e permitida aqui.
            raizes.add((no.module or ".").split(".")[0])
    return raizes


_RAIZES = _raizes_importadas(_CODIGO_FONTE)
check(
    _RAIZES <= _MODULOS_RAIZ_PERMITIDOS,
    f"21a. (PROVA A/AST) imports diretos de contratos.py vem so de "
    f"{sorted(_MODULOS_RAIZ_PERMITIDOS)} (encontrados={sorted(_RAIZES)})",
)

# PROVA B - DELTA de runtime. Tira snapshot de `sys.modules` DEPOIS de importar
# `pydantic` e ANTES de importar o contrato; o que interessa e so o que entrou
# entre os dois pontos. Se `requests` ja veio junto do Pydantic, esta no
# baseline e nao conta. Se aparecer so depois, foi `contratos.py` que trouxe.
_codigo_filho = (
    "import sys\n"
    f"sys.path.insert(0, {str(CONVERSAS_DIR)!r})\n"
    "import pydantic  # baseline: tudo que o proprio Pydantic arrasta\n"
    "baseline = set(sys.modules)\n"
    "import app.v2.contratos\n"
    "delta = set(sys.modules) - baseline\n"
    f"suspeitos = {_SUSPEITOS_IMPORT!r}\n"
    "novos = sorted(m for m in suspeitos if m in delta)\n"
    "print('NOVOS:' + (','.join(novos) if novos else 'NENHUM'))\n"
)
_resultado_subproc = subprocess.run(
    [sys.executable, "-c", _codigo_filho],
    capture_output=True, text=True, timeout=30,
)
check(
    _resultado_subproc.returncode == 0,
    f"21b. subprocesso limpo importa app.v2.contratos sem erro "
    f"(rc={_resultado_subproc.returncode}, stderr={_resultado_subproc.stderr!r})",
)
_saida_subproc = _resultado_subproc.stdout.strip()
check(
    _saida_subproc == "NOVOS:NENHUM",
    f"21c. (PROVA B/DELTA) nenhum suspeito NOVO alem do baseline do pydantic "
    f"entra em sys.modules ao importar app.v2.contratos - mede o delta, nao a "
    f"presenca absoluta (suspeitos={_SUSPEITOS_IMPORT}, saida={_saida_subproc!r}, "
    f"stderr={_resultado_subproc.stderr!r})",
)


# --- 22. DomainEvent nao existe no modulo (removido, nao diferido) --------
check(
    not hasattr(_contratos_mod, "DomainEvent"),
    "22. DomainEvent NAO existe em app.v2.contratos (removido - ver docstring do modulo)",
)


# --- 23. nenhum contrato diferido para fases futuras existe ainda ---------
_DEFERIDOS = [
    "InboundContextV2", "AIInterpretationRequest", "RuleEngineInput",
    "RuleEngineDecision", "AuthorizedResponse", "HandoffRequest",
    "HandoffResult", "MensagemLote",
]
for _nome in _DEFERIDOS:
    check(
        not hasattr(_contratos_mod, _nome),
        f"23. contrato diferido '{_nome}' ainda NAO existe (Fase 1 e so os 6 modelos)",
    )


# --- 24. nenhum validator de negocio mora neste modulo ---------------------
check(
    not any(
        termo in _CODIGO_FONTE
        for termo in ("field_validator", "model_validator", "AfterValidator", "BeforeValidator")
    ),
    "24a. codigo-fonte do modulo nao referencia field_validator/model_validator/AfterValidator/BeforeValidator",
)
for _nome, _cls in MODELOS.items():
    _decorators = _cls.__pydantic_decorators__
    _sem_validators = (
        not _decorators.validators
        and not _decorators.field_validators
        and not _decorators.model_validators
    )
    check(_sem_validators, f"24b. {_nome} nao tem nenhum validator Pydantic registrado (__pydantic_decorators__)")


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
