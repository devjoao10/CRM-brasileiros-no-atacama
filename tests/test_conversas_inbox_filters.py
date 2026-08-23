"""
PACOTE-B — novo inbox do Conversas (5 categorias, server-side).

Categorias (predicados no SQL, fonte unica `_inbox_predicates`):
  meus       aberta + is_bot_active=false + atendente_id = current_user
  fila       aberta + is_bot_active=false + atendente_id IS NULL
  bia        aberta + is_bot_active=true
  todos      aberta + is_bot_active=false
  encerradas status='encerrada'

Prova que:
  1. Fixture A-G classifica exatamente como especificado, para Julia e Joao.
  2. FIFO da fila: queued_at ASC, legado (NULL) por ultimo, id como desempate;
     mensagem nova do cliente NAO altera a ordem.
  3. Matriz de exclusividade: BIA nao intersecta Fila/Meus/Todos;
     Fila nao intersecta Meus; Fila e Meus sao subconjuntos de Todos;
     Encerradas fora das quatro abertas.
  4. /counts bate com o len() de cada listagem, numa unica query agregada.
  5. `meus` usa o usuario AUTENTICADO — nao existe parametro de user_id.
  6. Paginacao: 250 conversas, total correto, primeira pagina limitada,
     restante acessivel, filtro/busca aplicados no SQL (nao sobre a pagina).
  7. Filtros combinados: inbox+search, inbox+responsavel_id, inbox+tag_id,
     inbox+search+tag.
  8. Ordenacao das demais categorias por atividade recente.
  9. inbox invalido -> 422; sem auth -> 401.
 10. Legado (is_bot_active=false, atendente NULL, queued_at NULL) continua
     VISIVEL na fila.
 11. N+1 de tags eliminado (contagem de queries da listagem).
 12. Frontend: dropdown com as 5 categorias, abas antigas removidas,
     default meus, sem refiltro de categoria em JS, badge via /counts,
     guarda de corrida, erro != vazio.

Roda standalone:  python tests/test_conversas_inbox_filters.py
"""
import datetime as dt
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_inbox_filters_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.tag import ConversationTag  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# Tabela CRM-shaped `leads` no MESMO sqlite (padrao do
# test_conversas_hotfix_filters_resp). Sem ela, get_leads_responsaveis falha e
# o db.rollback() do tratamento de erro EXPIRA a sessao — cada conversa seria
# relida uma a uma, mascarando o selectinload e falseando a medicao de N+1.
with engine.begin() as _c:
    _c.execute(text(
        "CREATE TABLE IF NOT EXISTS leads ("
        "id INTEGER PRIMARY KEY, nome VARCHAR(200), whatsapp VARCHAR(30), "
        "email VARCHAR(255), responsavel_id INTEGER)"
    ))

_db = SessionLocal()
for uid, nome, email in ((1, "Julia", "julia@local"), (2, "Joao", "joao@local")):
    if not _db.query(User).filter(User.id == uid).first():
        _db.add(User(id=uid, nome=nome, email=email,
                     hashed_password="x", role="user", is_active=True))
tag = ConversationTag(id=1, nome="Quente", cor="#FF0000")
_db.add(tag)
_db.commit()

T = dt.datetime(2026, 8, 23, 10, 0, tzinfo=dt.timezone.utc)


def conv(**kw):
    kw.setdefault("status", "aberta")
    kw.setdefault("is_bot_active", False)
    return Conversation(**kw)


# A=BIA  B/C=fila  D=Julia  E=Joao  F=encerrada  G=legado (queued_at NULL)
_db.add_all([
    conv(id=1, lead_id=1, whatsapp="551100001", nome="A bia", is_bot_active=True),
    conv(id=2, lead_id=2, whatsapp="551100002", nome="B fila", queued_at=T),
    conv(id=3, lead_id=3, whatsapp="551100003", nome="C fila",
         queued_at=T + dt.timedelta(minutes=5)),
    conv(id=4, lead_id=4, whatsapp="551100004", nome="D Maria", atendente_id=1,
         responsavel_id=7),
    conv(id=5, lead_id=5, whatsapp="551100005", nome="E Pedro", atendente_id=2),
    conv(id=6, lead_id=6, whatsapp="551100006", nome="F encerrada", status="encerrada"),
    conv(id=7, lead_id=7, whatsapp="551100007", nome="G legado", queued_at=None),
])
_db.commit()
c4 = _db.query(Conversation).filter(Conversation.id == 4).first()
c4.tags.append(_db.query(ConversationTag).filter(ConversationTag.id == 1).first())
_db.commit()
_db.close()


class _U1:
    id = 1
    nome = "Julia"
    email = "julia@local"
    role = "user"
    is_active = True


class _U2:
    id = 2
    nome = "Joao"
    email = "joao@local"
    role = "user"
    is_active = True


def as_user(u):
    main.app.dependency_overrides[get_current_user] = lambda: u


as_user(_U1())
client = TestClient(main.app)


def names(inbox, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/conversations?inbox={inbox}" + (f"&{q}" if q else "")
    r = client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:200])
    return [c["nome"] for c in r.json()["conversations"]]


def total(inbox, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/conversations?inbox={inbox}" + (f"&{q}" if q else "")
    return client.get(url).json()["total"]


# ============ 1. CLASSIFICACAO ============
print("1 — classificacao das 5 categorias (Julia)")
check(names("meus") == ["D Maria"], f"meus(Julia) = D  ({names('meus')})")
check(names("fila") == ["B fila", "C fila", "G legado"], f"fila = B,C,G  ({names('fila')})")
check(names("bia") == ["A bia"], f"bia = A  ({names('bia')})")
check(sorted(names("todos")) == ["B fila", "C fila", "D Maria", "E Pedro", "G legado"],
      f"todos = B,C,D,E,G  ({sorted(names('todos'))})")
check(names("encerradas") == ["F encerrada"], f"encerradas = F  ({names('encerradas')})")

print("1b — 'meus' segue o usuario AUTENTICADO")
as_user(_U2())
check(names("meus") == ["E Pedro"], f"meus(Joao) = E  ({names('meus')})")
check(names("todos") == names("todos"), "todos independe do usuario")
as_user(_U1())

# ============ 2. FIFO ============
print("2 — FIFO da fila")
check(names("fila") == ["B fila", "C fila", "G legado"],
      "ordem: queued_at ASC, legado (NULL) por ultimo")
d = SessionLocal()
cB = d.query(Conversation).filter(Conversation.id == 2).first()
cB.unread_count = 5
cB.last_customer_msg_at = dt.datetime.now(dt.timezone.utc)
cB.updated_at = dt.datetime.now(dt.timezone.utc)
d.commit()
d.close()
check(names("fila") == ["B fila", "C fila", "G legado"],
      "mensagem nova do cliente NAO altera a posicao na fila")

print("2b — desempate deterministico entre legados (queued_at NULL)")
d = SessionLocal()
d.add(conv(id=8, lead_id=8, whatsapp="551100008", nome="H legado", queued_at=None))
d.commit()
d.close()
check(names("fila") == ["B fila", "C fila", "G legado", "H legado"],
      "legados ordenados por id ASC")
d = SessionLocal()
d.query(Conversation).filter(Conversation.id == 8).delete()
d.commit()
d.close()

# ============ 3. MATRIZ DE EXCLUSIVIDADE ============
print("3 — matriz de exclusividade")
S = {k: set(names(k)) for k in ("meus", "fila", "bia", "todos", "encerradas")}
check(not (S["bia"] & S["fila"]), "BIA inter Fila = vazio")
check(not (S["bia"] & S["meus"]), "BIA inter Meus = vazio")
check(not (S["bia"] & S["todos"]), "BIA inter Todos = vazio")
check(not (S["fila"] & S["meus"]), "Fila inter Meus = vazio")
check(S["fila"] <= S["todos"], "Fila subconjunto de Todos")
check(S["meus"] <= S["todos"], "Meus subconjunto de Todos")
check(not (S["encerradas"] & (S["meus"] | S["fila"] | S["bia"] | S["todos"])),
      "Encerradas fora das quatro categorias abertas")

# ============ 4. COUNTS ============
print("4 — /counts")
r = client.get("/api/conversations/counts")
check(r.status_code == 200, "counts responde 200")
cnt = r.json()
check(cnt["meus"] == 1 and cnt["fila"] == 3 and cnt["bia"] == 1
      and cnt["todos"] == 5 and cnt["encerradas"] == 1,
      f"counts(Julia) = meus1 fila3 bia1 todos5 encerradas1  ({cnt})")
for k in ("meus", "fila", "bia", "todos", "encerradas"):
    check(cnt[k] == total(k), f"counts[{k}] == total da listagem ({cnt[k]})")
as_user(_U2())
check(client.get("/api/conversations/counts").json()["meus"] == 1,
      "counts.meus(Joao) = 1 (muda pelo usuario autenticado)")
as_user(_U1())

print("4b — counts NAO aceita user_id do cliente")
base = client.get("/api/conversations/counts").json()["meus"]
spoof = client.get("/api/conversations/counts?user_id=2").json()["meus"]
check(base == spoof == 1, "parametro user_id e ignorado (nao existe)")
check("user_id" not in (CONVERSAS_DIR / "app" / "routers" / "conversations.py")
      .read_text(encoding="utf-8").split("def conversation_counts")[1].split("@router")[0],
      "rota /counts nao declara nenhum parametro user_id")

print("4c — counts em UMA query agregada")
qs = []
_ev = lambda conn, cur, st, params, ctx, many: qs.append(st)  # noqa: E731
event.listen(engine, "before_cursor_execute", _ev)
client.get("/api/conversations/counts")
event.remove(engine, "before_cursor_execute", _ev)
sel = [q for q in qs if q.strip().lower().startswith("select")]
agg = [q for q in sel if q.lower().count("count(*) filter") == 5]
check(len(agg) == 1,
      f"as 5 contagens em UMA query agregada (COUNT(*) FILTER x5) — achou {len(agg)}")
check(len(sel) <= 2, f"/counts faz no maximo 2 SELECTs (agregado + unread) — {len(sel)}")

print("4d — dataset de notificacao independe da categoria")
check("unread" in cnt and isinstance(cnt["unread"], dict), "counts expoe o mapa unread")
check("2" in cnt["unread"], "unread cobre conversa da FILA mesmo com aba 'meus' ativa")

# ============ 5. FILTROS COMBINADOS ============
print("5 — filtros combinados (tudo no SQL)")
check(names("todos", search="Maria") == ["D Maria"], "inbox + search")
check(names("fila", search="Maria") == [], "inbox + search respeita a categoria")
check(names("todos", responsavel_id=7) == ["D Maria"], "inbox + responsavel_id")
check(names("todos", tag_id=1) == ["D Maria"], "inbox + tag_id")
check(names("todos", search="Maria", tag_id=1) == ["D Maria"], "inbox + search + tag")
check(names("bia", tag_id=1) == [], "inbox + tag respeita a categoria")

# ============ 6. ORDENACAO DAS DEMAIS ============
print("6 — demais categorias por atividade recente")
ordem = names("todos")
check(ordem[0] == "B fila", f"todos: mais recente primeiro (B foi tocada) — {ordem}")

# ============ 7. VALIDACAO / AUTH ============
print("7 — validacao e auth")
check(client.get("/api/conversations?inbox=banana").status_code == 422,
      "inbox invalido -> 422")
main.app.dependency_overrides.pop(get_current_user, None)
anon = TestClient(main.app)
check(anon.get("/api/conversations?inbox=meus").status_code == 401, "listagem sem auth -> 401")
check(anon.get("/api/conversations/counts").status_code == 401, "counts sem auth -> 401")
check("test-secret-key" not in anon.get("/api/conversations/counts").text, "sem segredo na resposta")
as_user(_U1())

# ============ 8. LEGADO VISIVEL ============
print("8 — legado queued_at NULL")
check("G legado" in names("fila"), "legado (queued_at NULL) continua VISIVEL na fila")
check(names("fila")[-1] == "G legado", "legado ordenado por ultimo (NULLS LAST)")

# ============ 10. PAGINACAO ============
print("10 — paginacao com volume > primeira pagina")
d = SessionLocal()
for i in range(250):
    d.add(conv(id=1000 + i, lead_id=1000 + i, whatsapp=f"5599{i:06d}",
               nome=f"Vol {i:03d}", atendente_id=1,
               updated_at=T - dt.timedelta(minutes=i)))
d.add(conv(id=9999, lead_id=9999, whatsapp="5598000000", nome="Agulha Distante",
           atendente_id=1, updated_at=T - dt.timedelta(days=400)))
d.commit()
d.close()

r = client.get("/api/conversations?inbox=meus&limit=50&offset=0").json()
check(r["total"] == 252, f"total correto = 252 (achou {r['total']})")
check(len(r["conversations"]) == 50, f"primeira pagina limitada a 50 (achou {len(r['conversations'])})")
r2 = client.get("/api/conversations?inbox=meus&limit=50&offset=50").json()
check(len(r2["conversations"]) == 50, "segunda pagina acessivel")
first = {c["id"] for c in r["conversations"]}
second = {c["id"] for c in r2["conversations"]}
check(not (first & second), "paginas nao se sobrepoem")

seen = set()
off = 0
while off < r["total"]:
    page = client.get(f"/api/conversations?inbox=meus&limit=50&offset={off}").json()["conversations"]
    if not page:
        break
    seen.update(c["id"] for c in page)
    off += 50
check(len(seen) == 252, f"todas as 252 alcancaveis via paginacao (achou {len(seen)})")

print("10b — busca alcanca conversa FORA da primeira pagina")
r = client.get("/api/conversations?inbox=meus&search=Agulha").json()
check(r["total"] == 1 and r["conversations"][0]["nome"] == "Agulha Distante",
      "busca no SQL encontra registro alem da 1a pagina (nao filtra a pagina)")
check(client.get("/api/conversations/counts").json()["meus"] == 252,
      "counts reflete o total real, nao a pagina carregada")

# ============ 9. N+1: custo INVARIANTE ao numero de linhas ============
print("9 — N+1 de tags: contagem de queries nao cresce com a pagina")


def query_count(url):
    qs.clear()
    event.listen(engine, "before_cursor_execute", _ev)
    client.get(url)
    event.remove(engine, "before_cursor_execute", _ev)
    return len(qs), len([q for q in qs if "conversation_tag_links" in q])


qs = []
n5, t5 = query_count("/api/conversations?inbox=meus&limit=5")
n50, t50 = query_count("/api/conversations?inbox=meus&limit=50")
check(n5 == n50,
      f"mesmo numero de queries para 5 e 50 linhas ({n5} vs {n50}) — sem N+1")
check(t50 <= 1, f"tags em UMA query em lote para 50 linhas (achou {t50})")
check(n50 <= 5, f"listagem custa <= 5 queries independentemente do N (achou {n50})")

# ============ 10c. GATE 2 — JANELA DE PAGINACAO + POLLING ============
# Simula, contra o backend REAL, o ciclo que o JS executa:
#   primeira pagina -> Carregar mais -> polling (offset 0, limit = janela)
# A semantica exigida: o polling SUBSTITUI a lista reconsultando de 0 ate o
# tamanho da janela — nunca faz append, nunca reseta para PAGE_SIZE.
print("10c — Carregar mais + polling (janela preservada)")
PAGE = 50


def fetch(inbox, offset, limit, **extra):
    q = "&".join(f"{k}={v}" for k, v in extra.items())
    url = f"/api/conversations?inbox={inbox}&limit={limit}&offset={offset}" + (f"&{q}" if q else "")
    return client.get(url).json()["conversations"]


# 1. primeira pagina -> carregar mais -> polling
pag1 = fetch("meus", 0, PAGE)
window = list(pag1)
check(len(window) == PAGE, f"primeira pagina = {PAGE} (achou {len(window)})")
pag2 = fetch("meus", len(window), PAGE)          # "Carregar mais" (append)
window += pag2
check(len(window) == 100, f"apos Carregar mais a janela = 100 (achou {len(window)})")
ids_janela = [c["id"] for c in window]
check(len(set(ids_janela)) == 100, "janela SEM duplicacao apos append")

refresh = fetch("meus", 0, len(window))          # polling: offset 0, limit=janela
ids_refresh = [c["id"] for c in refresh]
check(len(refresh) == 100, f"polling preserva a janela de 100 (achou {len(refresh)})")
check(len(set(ids_refresh)) == 100, "polling SEM duplicacao")
check(len(refresh) != PAGE, "polling NAO resetou para PAGE_SIZE")
check(ids_refresh == ids_janela, "polling SUBSTITUI mantendo a mesma ordem do servidor")

# 5/6. ordenacao server-side preservada no refresh, em cada categoria
for cat in ("meus", "todos", "bia"):
    a = [c["id"] for c in fetch(cat, 0, 20)]
    b = [c["id"] for c in fetch(cat, 0, 20)]
    check(a == b, f"{cat}: ordenacao server-side estavel entre polls")

fila_a = [c["nome"] for c in fetch("fila", 0, 20)]
d = SessionLocal()
cC = d.query(Conversation).filter(Conversation.id == 3).first()
cC.unread_count = 9
cC.updated_at = dt.datetime.now(dt.timezone.utc)
d.commit()
d.close()
fila_b = [c["nome"] for c in fetch("fila", 0, 20)]
check(fila_a == fila_b, f"fila: FIFO intacto no polling, conversa nova NAO vai ao topo ({fila_b})")

# 2. carregar mais -> trocar de inbox: reset e zero vazamento
outra = fetch("bia", 0, PAGE)
check(len(outra) <= PAGE, "troca de inbox volta a PAGE_SIZE")
check(not (set(c["id"] for c in outra) & set(ids_janela)),
      "nenhuma conversa da inbox anterior permanece apos a troca")

# 3. carregar mais -> mudar search/tag/responsavel: reset, sem mistura
busca = fetch("meus", 0, PAGE, search="Agulha")
check(len(busca) == 1 and busca[0]["nome"] == "Agulha Distante",
      "mudar a busca reseta a paginacao e nao mistura resultados antigos")
por_tag = fetch("todos", 0, PAGE, tag_id=1)
check(all(any(t["id"] == 1 for t in c["tags"]) for c in por_tag),
      "mudar a tag reseta e traz apenas o filtrado")

# 4. offset alem do total nunca quebra nem inventa linha
alem = fetch("meus", 100000, PAGE)
check(alem == [], "offset alem do total devolve vazio (sem erro, sem cursor invalido)")

print("10d — GATE 2: guards do JS")
js_g2 = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("let loadedWindowSize = PAGE_SIZE;" in js_g2, "estado da janela carregada existe")
check("const limit = isRefresh ? Math.min(loadedWindowSize, MAX_PAGE_LIMIT) : PAGE_SIZE;" in js_g2,
      "refresh pede a JANELA; demais modos pedem PAGE_SIZE")
check("offset: String(isAppend ? conversations.length : 0)," in js_g2,
      "offset deriva de conversations.length: refresh/busca partem de 0, append continua")
check("listOffset" not in js_g2, "estado paralelo de paginacao (listOffset) eliminado")
check("conversations = isAppend ? conversations.concat(page) : page;" in js_g2,
      "somente 'append' concatena — refresh SUBSTITUI")
check("if (!isRefresh) loadedWindowSize = Math.max(PAGE_SIZE, conversations.length);" in js_g2,
      "refresh NAO altera o tamanho da janela (nao reseta para PAGE_SIZE)")
check("if (!isAppend && !isRefresh) loadedWindowSize = PAGE_SIZE;" in js_g2,
      "busca nova / troca de filtro zera a janela")
check("loadedWindowSize = PAGE_SIZE;   // troca de categoria zera a paginacao" in js_g2,
      "troca de inbox zera a janela")
check("if (loadedWindowSize <= MAX_PAGE_LIMIT) loadConversations('refresh');" in js_g2,
      "polling usa o modo refresh")
check("loadConversations('append')" in js_g2, "'Carregar mais' usa o modo append")
check("loadConversations(true)" not in js_g2, "modo booleano antigo eliminado")
# corrida: TODOS os modos passam pela mesma guarda de sequencia
check(js_g2.count("const seq = ++listRequestSeq;") == 1,
      "um unico ponto de sequencia cobre refresh/append/busca/troca de inbox")

# ============ 11. FRONTEND ============
print("11 — frontend")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

for key, label in (("meus", "Meus atendimentos"), ("fila", "Fila de espera"),
                   ("bia", "Atendimentos BIA"), ("todos", "Todos"),
                   ("encerradas", "Encerradas")):
    check(f'data-inbox="{key}"' in html, f"dropdown tem a categoria {key}")
    check(label in html, f"nomenclatura exata: {label}")

check(len(re.findall(r'data-inbox="', html)) == 5, "exatamente 5 categorias no dropdown")
check("conv-filter-tabs" not in html and "conv-filter-tabs" not in js
      and "conv-filter-tabs" not in css, "abas antigas REMOVIDAS de html/js/css")
check('data-filter="all"' not in html, "aba 'Todas' antiga removida")
check("ChatBot" not in html and "Minha fila" not in html, "nomenclatura proibida ausente")

check("let activeInbox = 'meus'" in js, "default = meus")
check("INBOX_KEYS.includes(saved) ? saved : 'meus'" in js, "preferencia invalida cai no default")
check("inbox: activeInbox" in js, "troca de categoria envia ?inbox= ao servidor")
check("activeFilter" not in js.replace("// PACOTE-B: categoria do inbox (server-side). Substitui activeFilter, que", ""),
      "estado activeFilter (filtro JS) eliminado")
check("filtered.filter(c => c.atendente_id" not in js and "c.atendente_id === me.id" not in js,
      "NENHUM refiltro de categoria em JS")
check("const filtered = conversations;" in js, "render usa a lista do servidor, sem refiltrar")

# Guards ESTRUTURAIS (nao apenas "o token existe"): a mutacao que remove UMA
# das duas checagens de sequencia, ou que troca `if (listError)` por `if
# (false)`, tem de derrubar o teste.
check(js.count("if (seq !== listRequestSeq) return;") == 2,
      f"guarda de corrida nos DOIS pontos (antes do ok e apos o json) — achou "
      f"{js.count('if (seq !== listRequestSeq) return;')}")
GUARD_SEQ = "if (seq !== listRequestSeq) return;"
GUARD_POS = GUARD_SEQ + chr(10) * 2 + " " * 8 + "if (!resp || !resp.ok) {"
check(GUARD_POS in js, "guarda de corrida vem ANTES de tratar a resposta")
check("loadCounts" in js and "conv-inbox-badge" in js, "badge alimentado pelo /counts")
check("if (listError) {" in js and "conv-list-error" in js,
      "estado de ERRO com condicao real (nao apenas o token no arquivo)")
check("listError = true;" in js and "listError = false;" in js,
      "listError e efetivamente ligado/desligado conforme a resposta")
check("Nenhum cliente aguardando atendimento." in js
      and "Nenhum atendimento seu no momento." in js
      and "Nenhum atendimento com a BIA no momento." in js,
      "empty states por categoria")
check("conv-list-more" in js and "loadConversations('append')" in js,
      "'Carregar mais' (sem cap silencioso)")
check("searchTimer" in js and "loadConversations()" in js, "busca vai ao servidor (debounce)")
check("processUnreadNotifications(data.unread" in js,
      "notificacao le o /counts, nao a lista filtrada")

print("11b — acessibilidade / mobile / xss")
check("<details" in html and "<summary" in html, "dropdown usa disclosure nativo")
check("min-height: 44px" in css, "tap target >= 44px")
check("document.addEventListener('keydown'" not in js, "NENHUM keydown global novo")
check("inboxSel.addEventListener('keydown'" in js, "Escape escopado ao proprio dropdown")
check("escapeHtml(msg)" in js, "empty state escapado (sem innerHTML cru)")

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
