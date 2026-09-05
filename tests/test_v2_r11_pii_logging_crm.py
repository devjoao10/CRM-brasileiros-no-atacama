# -*- coding: utf-8 -*-
"""
BIA-V2 R11 - auditoria de PII exposure em logging (lado CRM).

Escopo da auditoria R11: todo `logger.*`/`logging.*`/`print(...)` do caminho
executavel da BIA V2. Do lado CRM isso e `app/routers/v2_lead_domain.py` (e
qualquer coisa que ele chame para logar - nao ha nenhuma: o router fala com a
Session direto, sem camada de servico intermediaria).

O router tem exatamente DOIS call sites de log, os dois `logger.info`:
  - `_responder_replay` ("handoff replay: ...")
  - fim de `aplicar_handoff` ("handoff aplicado: ...")

Os dois interpolam SO ids (`event_id`, `lead_id`, `target_user_id`,
`responsavel_atual`/`current_user.id`) - nunca um objeto `Lead`/`User` inteiro
nem um campo de PII deles. Este teste prova isso com um `Lead` e um `User`
cujo nome/telefone/email sao PROPOSITALMENTE PII-shaped (dado FALSO, nunca
real) - se um dia um call site trocar `lead.id` por `lead`/`lead.nome`, este
teste acusa.

Teste do CRM (job Python 3.11 do CI). Nao insere nada no sys.path do
Conversas e nao importa nada de la.

Roda standalone: python tests/test_v2_r11_pii_logging_crm.py
"""
import logging
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_r11_pii_logging_crm_test.db"
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

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.auth import hash_api_key  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


class _ListaHandler(logging.Handler):
    """Handler minimo que acumula as linhas formatadas de UM logger nomeado.

    Nao depende de `configurar_logging()`/redirect de stdout (o padrao ja
    provado em tests/test_v2_logging.py para o Conversas) - anexado direto no
    logger do modulo sob teste, e removido no fim, para nao interferir no
    resto do processo.
    """

    def __init__(self):
        super().__init__()
        self.linhas = []

    def emit(self, record):
        self.linhas.append(self.format(record))


def capturar_logs(logger_name, fn):
    """Roda `fn()` com um handler anexado ao logger `logger_name` e devolve
    `(texto_logado, retorno_de_fn)` - precisamos das DUAS coisas: o texto para
    a prova de PII, o retorno para confirmar que a chamada teve o efeito
    esperado (sanidade do proprio teste)."""
    logger = logging.getLogger(logger_name)
    handler = _ListaHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    nivel_antigo = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        retorno = fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(nivel_antigo)
    return "\n".join(handler.linhas), retorno


LOGGER_NAME = "app.routers.v2_lead_domain"

Base.metadata.create_all(bind=engine)

API_KEY = "bna_chave-teste-r11-pii"
HEADERS = {"X-API-Key": API_KEY}

# Dado FALSO, propositalmente PII-shaped (nunca dado real) - a prova inteira
# depende de este texto NAO ser generico o bastante para aparecer por acaso.
NOME_LEAD_PII_FALSO = "Fulano PII de Teste"
EMAIL_LEAD_PII_FALSO = "fulano.pii.teste@exemplo-fake.test"
TELEFONE_LEAD_PII_FALSO = "+5511999990000"
NOME_ATENDENTE_PII_FALSO = "Atendente PII de Teste"
EMAIL_ATENDENTE_PII_FALSO = "atendente.pii.teste@exemplo-fake.test"

_db = SessionLocal()
_integrador = User(
    nome="Integrador", email="integrador@teste.local", hashed_password="x",
    role=UserRole.USER, is_active=True, email_verified=True,
    api_key=hash_api_key(API_KEY),
)
_atendente = User(
    nome=NOME_ATENDENTE_PII_FALSO, email=EMAIL_ATENDENTE_PII_FALSO, hashed_password="x",
    role=UserRole.USER, is_active=True, email_verified=True,
)
_db.add_all([_integrador, _atendente])
_db.commit()
INTEGRADOR_ID, ATENDENTE_ID = _integrador.id, _atendente.id
_db.close()

# Achado 1 (security-reviewer, ver tests/test_v2_lead_domain.py): so o usuario
# de servico configurado pode chamar o handoff. Sobrescrito PELO MODULO -
# `get_servico_handoff_user` le `config.V2_LEAD_DOMAIN_SERVICE_USER_ID` em
# tempo de chamada.
config.V2_LEAD_DOMAIN_SERVICE_USER_ID = INTEGRADOR_ID

_db2 = SessionLocal()
_lead = Lead(nome=NOME_LEAD_PII_FALSO, whatsapp=TELEFONE_LEAD_PII_FALSO, email=EMAIL_LEAD_PII_FALSO)
_db2.add(_lead)
_db2.commit()
LEAD_ID = _lead.id
_db2.close()

client = TestClient(app)


def handoff(event_id, lead_id, target_user_id):
    return client.post(
        "/api/v2/lead-domain/handoff",
        json={"event_id": event_id, "lead_id": lead_id, "target_user_id": target_user_id},
        headers=HEADERS,
    )


# ── 1. Primeira aplicacao — logger.info de sucesso (fim de aplicar_handoff) ─

print("\n1. logger.info de sucesso (aplicar_handoff) nao vaza PII do lead/atendente")
event_id_1 = str(uuid.uuid4())
saida_1, resp_1 = capturar_logs(LOGGER_NAME, lambda: handoff(event_id_1, LEAD_ID, ATENDENTE_ID))

check(resp_1.status_code == 200, f"1-sanity. handoff aplicado com 200 (veio {resp_1.status_code}: {resp_1.text[:200]})")
check("handoff aplicado" in saida_1, "1a. o log de sucesso foi de fato emitido")
check(str(LEAD_ID) in saida_1, "1b. lead_id (id) aparece no log")
check(event_id_1 in saida_1, "1c. event_id aparece no log")
check(str(ATENDENTE_ID) in saida_1, "1d. target_user_id (id do atendente) aparece no log")
check(str(INTEGRADOR_ID) in saida_1, "1e. user_id de quem chamou (id) aparece no log")
check(NOME_LEAD_PII_FALSO not in saida_1, "1f. nome do LEAD (PII-falso) NAO aparece no log")
check(EMAIL_LEAD_PII_FALSO not in saida_1, "1g. email do LEAD (PII-falso) NAO aparece no log")
check(TELEFONE_LEAD_PII_FALSO not in saida_1, "1h. telefone do LEAD (PII-falso) NAO aparece no log")
check(NOME_ATENDENTE_PII_FALSO not in saida_1, "1i. nome do ATENDENTE (PII-falso) NAO aparece no log")
check(EMAIL_ATENDENTE_PII_FALSO not in saida_1, "1j. email do ATENDENTE (PII-falso) NAO aparece no log")

# ── 2. Replay do MESMO event_id — logger.info de _responder_replay ──────────

print("\n2. logger.info de replay (_responder_replay) nao vaza PII do lead/atendente")
saida_2, resp_2 = capturar_logs(LOGGER_NAME, lambda: handoff(event_id_1, LEAD_ID, ATENDENTE_ID))

check(resp_2.status_code == 200 and resp_2.json().get("replayed") is True,
      f"2-sanity. segunda chamada com o MESMO event_id e replay=true (veio {resp_2.status_code}: {resp_2.text[:200]})")
check("handoff replay" in saida_2, "2a. o log de replay foi de fato emitido")
check(event_id_1 in saida_2, "2b. event_id aparece no log de replay")
check(str(LEAD_ID) in saida_2, "2c. lead_id (id) aparece no log de replay")
check(str(ATENDENTE_ID) in saida_2, "2d. target_user_id (id) aparece no log de replay")
check(NOME_LEAD_PII_FALSO not in saida_2, "2e. nome do LEAD (PII-falso) NAO aparece no log de replay")
check(EMAIL_LEAD_PII_FALSO not in saida_2, "2f. email do LEAD (PII-falso) NAO aparece no log de replay")
check(TELEFONE_LEAD_PII_FALSO not in saida_2, "2g. telefone do LEAD (PII-falso) NAO aparece no log de replay")
check(NOME_ATENDENTE_PII_FALSO not in saida_2, "2h. nome do ATENDENTE (PII-falso) NAO aparece no log de replay")
check(EMAIL_ATENDENTE_PII_FALSO not in saida_2, "2i. email do ATENDENTE (PII-falso) NAO aparece no log de replay")


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
