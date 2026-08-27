"""
AUDIT-2026-08-WG (F-503) — uma linha legada com `dados` NULL derrubava o
historico INTEIRO do lead.

`LeadHistory.dados` e `Column(JSON, default=dict)`: o default e do lado Python,
entao a coluna e NULL-avel e qualquer escrita fora da ORM — reparo manual via
psql, restore de dump, SQL cru, codigo antigo — grava NULL. O schema declarava
`dados: dict` NAO-opcional, entao o Pydantic levantava ValidationError na
serializacao e `GET /api/pipeline/history/{lead_id}` devolvia 500. Nao para
aquela linha: para a resposta toda. O timeline do lead ficava inacessivel e
"Ver no Funil" abria numa tela quebrada.

Ja houve um incidente exatamente assim (AUDIT-2026-08-W2F/F9), corrigido do
lado de quem ESCREVE. Este arquivo trava o lado de quem LE — o unico que
sobrevive a uma linha legada que ninguem pode reescrever daqui.

Roda standalone:  python tests/test_pipeline_history_legado.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "pipeline_history_legado_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import get_current_user  # noqa: E402
from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.pipeline import LeadHistory  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.schemas.pipeline import HistoryResponse  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

db = SessionLocal()
operador = User(nome="Operador", email="op@teste.local", hashed_password="x",
                role=UserRole.ADMIN, is_active=True, email_verified=True)
db.add(operador)
db.commit()

lead = Lead(nome="Cliente Legado", whatsapp="5511900000009")
db.add(lead)
db.commit()
LEAD_ID = lead.id

# Uma linha SADIA e uma linha LEGADA com `dados` NULL, escrita como o mundo
# real a escreve: SQL cru, sem passar pela ORM.
db.add(LeadHistory(lead_id=LEAD_ID, evento="created", descricao="normal",
                   dados={"origem": "teste"}))
db.commit()
db.execute(text(
    "INSERT INTO lead_history (lead_id, evento, descricao, dados) "
    "VALUES (:lid, 'responsavel_changed', 'legado sem dados', NULL)"
), {"lid": LEAD_ID})
db.commit()

nulos = db.execute(text(
    "SELECT count(*) FROM lead_history WHERE lead_id = :lid AND dados IS NULL"
), {"lid": LEAD_ID}).scalar()
db.close()

print("0 — a linha legada existe mesmo")
check(nulos == 1, f"ha exatamente 1 linha com dados NULL no banco (achou {nulos})")


print("\n1 — o schema normaliza NULL para objeto vazio")
r = HistoryResponse(id=1, lead_id=LEAD_ID, evento="x", dados=None)
check(r.dados == {}, f"dados=None vira {{}} (obteve {r.dados!r})")
r2 = HistoryResponse(id=2, lead_id=LEAD_ID, evento="x", dados={"a": 1})
check(r2.dados == {"a": 1}, "dados com conteudo continua intacto")
r3 = HistoryResponse(id=3, lead_id=LEAD_ID, evento="x")
check(r3.dados == {}, "dados ausente continua com o default {}")

# Normalizar NULL nao pode virar "aceitar qualquer coisa": dado MALFORMADO
# continua sendo erro. Esconder isso trocaria um 500 barulhento por corrupcao
# silenciosa.
try:
    HistoryResponse(id=4, lead_id=LEAD_ID, evento="x", dados="nao sou objeto")
    check(False, "dados com tipo errado deveria continuar sendo recusado")
except Exception as e:
    check("valid" in type(e).__name__.lower() or "Error" in type(e).__name__,
          f"dados com tipo errado continua recusado ({type(e).__name__})")


print("\n2 — o endpoint responde com a linha legada presente")


def _db_override():
    d = SessionLocal()
    try:
        yield d
    finally:
        d.close()


app.dependency_overrides[get_db] = _db_override
app.dependency_overrides[get_current_user] = lambda: operador
client = TestClient(app)

resp = client.get(f"/api/pipeline/history/{LEAD_ID}")
check(resp.status_code == 200,
      f"GET /api/pipeline/history/{{id}} devolve 200 (obteve {resp.status_code}) — "
      f"antes uma unica linha NULL derrubava a resposta inteira")

if resp.status_code == 200:
    corpo = resp.json()
    itens = corpo.get("historico", corpo if isinstance(corpo, list) else [])
    check(len(itens) == 2,
          f"as DUAS linhas voltam, a sadia e a legada (obteve {len(itens)})")
    dados = [i.get("dados") for i in itens]
    check(all(isinstance(d, dict) for d in dados),
          f"todo `dados` sai como objeto, nunca null (obteve {dados})")
    check({"origem": "teste"} in dados,
          "a linha sadia manteve o conteudo original")

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS TESTES DE HISTORICO LEGADO PASSARAM")
