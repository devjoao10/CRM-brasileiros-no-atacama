# -*- coding: utf-8 -*-
"""
Regressao — a janela padrao de datas de /api/analytics/{dashboard,reports}
precisa ser ancorada em UTC, nunca na hora local do processo do servidor.

`Lead.created_at` e `DateTime(timezone=True)` gravado em UTC
(`server_default=func.now()`). As duas rotas calculavam o fim da janela
padrao de "ultimos 30 dias" com `date.today()` — a data LOCAL do processo,
nao a data em UTC. Perto da virada do dia local no Brasil (UTC-3), um lead
criado entre ~21h e 23h59 local ja tem `created_at` gravado no dia SEGUINTE
em UTC, enquanto `date.today()` do servidor ainda devolve o dia anterior: a
janela padrao ["hoje - 29 dias", "hoje"] usava um `end_date` um dia atrasado
em relacao ao UTC do banco e excluia esse lead do relatorio — mesmo criado
"hoje".

O fix (nesta sessao, ver `git diff app/routers/analytics.py`) trocou
`date.today()` por `datetime.now(timezone.utc).date()` e tornou tz-aware os
`datetime.combine(...)` das bordas da janela, em quatro pontos do arquivo:
`get_dashboard_analytics` (end_date, hoje_end, start_dt/end_dt) e
`get_detailed_reports` (end_date, start_dt/end_dt).

Esta prova NAO depende da hora real do sistema no momento em que o teste
roda: `created_at` e um datetime UTC hardcoded, e tanto `datetime.now(
timezone.utc)` (o que o codigo corrigido chama) quanto `date.today()` (o que
o codigo antigo chamava) sao mockados dentro do modulo
`app.routers.analytics`, reproduzindo deterministicamente, em qualquer
maquina e qualquer fuso horario real do host:

  - "agora" = 2026-03-15 01:30 UTC — instante fixo e conhecido.
  - `date.today()` = 2026-03-14 — a data que um servidor rodando em
    America/Sao_Paulo (UTC-3) devolveria NESSE MESMO INSTANTE, ja que
    01:30 UTC de 15/03 e 22:30 local de 14/03.

Contra o codigo ANTIGO, o lead cai FORA da janela padrao (criado "amanha" em
relacao ao "hoje" local errado). Contra o codigo CORRIGIDO, o lead cai DENTRO
da janela padrao — o comportamento que o fix garante.

Rodar:  python tests/test_analytics_janela_utc.py
"""
import os
import pathlib
import sys
from datetime import date, datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
_DB_PATH = SCRATCH / "analytics_janela_utc.db"
if _DB_PATH.exists():
    try:
        _DB_PATH.unlink()
    except PermissionError:
        pass

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": f"sqlite:///{_DB_PATH.as_posix()}",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
    "SECRET_KEY": "test-secret-key",
})

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402

ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"

falhas = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHA'} {msg}")
    if not cond:
        falhas.append(msg)


# "Agora" fixo em UTC: 01:30 do dia 15 — ja e dia 15 em UTC, mas ainda
# 22:30 do dia 14 no horario de Brasilia (UTC-3). O cenario real do bug.
_AGORA_UTC = datetime(2026, 3, 15, 1, 30, 0, tzinfo=timezone.utc)
# A data LOCAL que `date.today()` devolveria nesse mesmo instante, num
# servidor rodando em America/Sao_Paulo: ainda o dia 14.
_HOJE_LOCAL_ERRADO = date(2026, 3, 14)


class _DatetimeCongelado(datetime):
    """Substitui `datetime` dentro de app.routers.analytics. `now(tz)` volta
    fixo em _AGORA_UTC (o que o codigo CORRIGIDO chama); `combine(...)`
    continua sendo o metodo real herdado, usado tanto pelo codigo corrigido
    quanto pelo antigo."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _AGORA_UTC.replace(tzinfo=None)
        return _AGORA_UTC.astimezone(tz)


class _DateCongelada(date):
    """Substitui `date` dentro de app.routers.analytics. So `today()` muda —
    e o que o codigo ANTIGO (pre-fix) chamava para calcular 'hoje'."""

    @classmethod
    def today(cls):
        return _HOJE_LOCAL_ERRADO


_C = {}


def _setup():
    """Cria o app em SQLite descartavel e UM lead com created_at hardcoded em
    UTC, direto via ORM (a API nunca aceita created_at). Cacheado: chamado
    pelos dois testes, cria o lead uma unica vez."""
    if _C:
        return _C["client"]

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        lead = Lead(nome="Lead fronteira UTC", is_active=True, created_at=_AGORA_UTC)
        db.add(lead)
        db.commit()
    finally:
        db.close()

    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login: {r.status_code} {r.text[:200]}"

    _C["client"] = client
    return client


def _chamar(client, path):
    with mock.patch("app.routers.analytics.datetime", _DatetimeCongelado), \
         mock.patch("app.routers.analytics.date", _DateCongelada):
        return client.get(path)


def test_dashboard_inclui_lead_da_fronteira_utc_na_janela_padrao():
    client = _setup()
    r = _chamar(client, "/api/analytics/dashboard")
    check(r.status_code == 200, f"GET /api/analytics/dashboard responde 200 (veio {r.status_code})")
    if r.status_code != 200:
        return
    body = r.json()
    check(body["end_date"] == _AGORA_UTC.date().isoformat(),
          f"end_date da janela padrao e a data UTC de 'agora' (veio {body['end_date']})")
    check(body["kpis"]["total_leads"] == 1,
          f"dashboard inclui o lead da fronteira UTC na janela padrao "
          f"(total_leads={body['kpis']['total_leads']})")


def test_reports_inclui_lead_da_fronteira_utc_na_janela_padrao():
    client = _setup()
    r = _chamar(client, "/api/analytics/reports")
    check(r.status_code == 200, f"GET /api/analytics/reports responde 200 (veio {r.status_code})")
    if r.status_code != 200:
        return
    body = r.json()
    check(body["kpis"]["total"] == 1,
          f"reports inclui o lead da fronteira UTC na janela padrao "
          f"(total={body['kpis']['total']})")


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for fn in ALL_TESTS:
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            falhas.append(fn.__name__)
            print(f"  ERROR {type(exc).__name__}: {exc}")
        print()
    print(f"{len(falhas)} falha(s)")
    sys.exit(1 if falhas else 0)
