# -*- coding: utf-8 -*-
"""
WP-R11-PII-02 - o handler de `HandoffIndeterminado` nao registra a excecao crua.

Complementa `tests/test_v2_r11_pii_logging_crm.py`, que audita os dois
`logger.info` de `app/routers/v2_lead_domain.py`. O ponto NAO coberto la e o
terceiro call site de log do caminho V2 do CRM: o exception handler registrado
na instancia FastAPI raiz, `app/main.py::_handoff_indeterminado_handler`.

POR QUE ELE PRECISA DE TESTE PROPRIO
--------------------------------------
`HandoffIndeterminado` e construida em `v2_lead_domain.py` (~L763) interpolando
`{exc}` da excecao original do SQLAlchemy. A `str()` de um `OperationalError`/
`IntegrityError` do SQLAlchemy embute o SQL COMPLETO e os PARAMETROS LIGADOS.
O handler registrava `str(exc)` E `exc_info=exc` (traceback, que carrega junto a
`__cause__` inteira) - ou seja, tudo isso ia para o log do servidor.

Uma auditoria desta rodada percorreu os statements por onde este endpoint pode
falhar e confirmou que os parametros de HOJE sao so ids: nao ha vazamento ativo.
Mas isso e uma coincidencia auditada, nao uma garantia - basta uma query nova
ligar um telefone ou um nome para o log passar a carrega-lo, em silencio. Este
teste transforma a coincidencia em invariante.

O teste usa uma causa com parametro PROPOSITALMENTE PII-shaped (dado FALSO,
nunca real) para que a falha, se voltar, seja obvia.

Teste do CRM (job Python 3.11 do CI). Nao toca o outro servico.

Roda standalone: python tests/test_v2_handoff_indeterminado_log.py
"""
import asyncio
import logging
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_handoff_indeterminado_log_test.db"
if DB_FILE.exists():
    try:
        DB_FILE.unlink()
    except PermissionError:
        pass

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""

sys.path.insert(0, str(ROOT))

from sqlalchemy.exc import OperationalError  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402

from app.main import _handoff_indeterminado_handler  # noqa: E402
from app.routers import v2_lead_domain  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


class _Captura(logging.Handler):
    """Guarda o registro JA FORMATADO - e o texto formatado que vai ao arquivo."""

    def __init__(self):
        super().__init__()
        self.textos = []

    def emit(self, record):
        texto = record.getMessage()
        if record.exc_info:
            texto += "\n" + logging.Formatter().formatException(record.exc_info)
        self.textos.append(texto)


def _request_falso(path):
    """Request minimo: o handler so le `request.url.path`."""
    return StarletteRequest(
        {"type": "http", "method": "POST", "path": path, "headers": [],
         "query_string": b"", "scheme": "http", "server": ("testserver", 80)}
    )


# ---------------------------------------------------------------------------
# A excecao REAL, montada como `v2_lead_domain.py` a monta: a mensagem da
# `HandoffIndeterminado` interpola a `str()` da causa do SQLAlchemy.
# ---------------------------------------------------------------------------
_TELEFONE_FALSO = "5511999887766"
_NOME_FALSO = "Fulano De Tal Sobrenome"
_SQL_FALSO = (
    "UPDATE leads SET responsavel_id=%(resp)s, telefone=%(tel)s, nome=%(nome)s "
    "WHERE leads.id = %(lid)s"
)

_causa = OperationalError(_SQL_FALSO, {"tel": _TELEFONE_FALSO, "nome": _NOME_FALSO}, Exception("boom"))
try:
    raise v2_lead_domain.HandoffIndeterminado(
        f"{type(_causa).__name__} nao esperado no handoff "
        f"event_id=e0000000-0000-0000-0000-000000000001: {_causa}"
    ) from _causa
except v2_lead_domain.HandoffIndeterminado as _e:
    _EXC = _e

check(
    _TELEFONE_FALSO in str(_EXC) and _NOME_FALSO in str(_EXC),
    "0. pre-condicao REAL: a `str()` da propria excecao de dominio CARREGA o SQL "
    "e os parametros ligados - sem isto o teste provaria o vazio",
)

# ---------------------------------------------------------------------------
# O handler roda; capturamos o que ele registrou.
# ---------------------------------------------------------------------------
_logger = logging.getLogger("app.main")
_captura = _Captura()
_nivel_original, _propaga_original = _logger.level, _logger.propagate
_logger.addHandler(_captura)
_logger.setLevel(logging.DEBUG)
try:
    _resposta = asyncio.run(
        _handoff_indeterminado_handler(_request_falso("/api/v2/lead-domain/handoff"), _EXC)
    )
finally:
    _logger.removeHandler(_captura)
    _logger.setLevel(_nivel_original)
    _logger.propagate = _propaga_original

_registrado = "\n".join(_captura.textos)

check(len(_captura.textos) == 1, f"1. o handler registra UMA linha (obteve {len(_captura.textos)})")

check(
    _TELEFONE_FALSO not in _registrado,
    f"2. o telefone dos parametros ligados NAO aparece no log [{_registrado[:160]}]",
)
check(
    _NOME_FALSO not in _registrado,
    f"3. o nome dos parametros ligados NAO aparece no log [{_registrado[:160]}]",
)
check(
    "UPDATE leads SET" not in _registrado,
    f"4. o SQL cru NAO aparece no log [{_registrado[:160]}]",
)
check(
    "Traceback" not in _registrado,
    "5. e nao ha traceback - era por ele que a `__cause__` (com o SQL) entrava "
    "mesmo que a mensagem fosse limpa",
)

# ---------------------------------------------------------------------------
# ...sem virar um log inutil: o que diagnostica continua la.
# ---------------------------------------------------------------------------
check(
    "/api/v2/lead-domain/handoff" in _registrado,
    f"6. a ROTA continua no log [{_registrado[:160]}]",
)
check(
    "HandoffIndeterminado" in _registrado,
    f"7. o TIPO da excecao de dominio continua no log [{_registrado[:160]}]",
)
check(
    "OperationalError" in _registrado,
    f"8. e o TIPO DA CAUSA tambem - e ele que distingue lock_timeout de conexao "
    f"caida de violacao de restricao [{_registrado[:160]}]",
)

# ---------------------------------------------------------------------------
# O contrato HTTP nao mudou.
# ---------------------------------------------------------------------------
check(_resposta.status_code == 500, f"9. a resposta continua 500 (obteve {_resposta.status_code})")
_corpo = _resposta.body.decode()
check(
    v2_lead_domain.TokenErroHandoff.HANDOFF_NAO_APLICADO in _corpo,
    f"10. e o corpo continua o token generico HANDOFF_NAO_APLICADO ({_corpo})",
)
check(
    _TELEFONE_FALSO not in _corpo and "UPDATE leads" not in _corpo,
    f"11. o corpo tambem nao carrega SQL nem parametro ({_corpo})",
)

print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("Todas as verificacoes passaram.")
