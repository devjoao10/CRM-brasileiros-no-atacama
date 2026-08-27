"""
AUDIT-2026-08-WG — BUG 4: guarda one-shot contra loop de login no Conversas.

conversas/templates/login.html espelha o formato validate-entao-redireciona
do CRM (chama /api/auth/me/validate e, se ok, navega para "/"), mas nao
tinha NENHUMA guarda sessionStorage contra redirecionamento repetido — ao
contrario do CRM, que quebra esse ciclo com uma guarda one-shot
(AUTH-LOOP-01: static/js/login.js + app/auth.py:233-238). Em um navegador
que derruba ou atrasa o cookie access_token (Safari ITP e o caso relatado —
cookie host-only, SameSite=Lax, Secure fora de dev), a pagina validava,
redirecionava, era devolvida por "/" e validava de novo — loop infinito.

Prova, em duas camadas:
  1. Fonte (estatico): a guarda existe, usa sessionStorage (nao
     localStorage — precisa morrer com a aba) e usa uma chave DISTINTA da
     chave do hub do CRM (crm_hub_hop), para os dois apps nao compartilharem
     estado de sessao.
  2. Comportamental (Node, quando disponivel): a segunda chegada a
     /login com a guarda ja marcada NAO redireciona de novo — limpa a sessao
     do cliente e deixa o formulario (ja no DOM) pronto para uso, em vez de
     tentar "/" outra vez.

Roda standalone:  python tests/test_conversas_login_loop_guard.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
LOGIN_TEMPLATE = CONVERSAS_DIR / "templates" / "login.html"

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


html = LOGIN_TEMPLATE.read_text(encoding="utf-8")

# ============ 1. Fonte — a guarda existe e usa chave propria ============
print("Login loop guard (Conversas) — guarda presente na fonte")

check("sessionStorage" in html, "login.html usa sessionStorage (guarda precisa morrer com a aba)")
check("conversas_inbox_hop" in html, "chave propria do hop guard presente (conversas_inbox_hop)")

crm_auth_js = (ROOT / "static" / "js" / "auth.js").read_text(encoding="utf-8")
m_crm = re.search(r"HOP_KEY:\s*'([^']+)'", crm_auth_js)
check(m_crm is not None, "chave HOP_KEY do CRM localizada em static/js/auth.js (para comparar)")
crm_hop_key = m_crm.group(1) if m_crm else None

# Compara o VALOR de fato atribuido a constante (nao uma busca de substring
# no arquivo inteiro) — um comentario explicando a decisao pode mencionar a
# chave do CRM pelo nome sem que isso signifique reuso de estado.
m_conv = re.search(r"const CONVERSAS_HOP_KEY\s*=\s*'([^']+)'", html)
check(m_conv is not None, "constante CONVERSAS_HOP_KEY declarada com uma string literal")
conv_hop_key = m_conv.group(1) if m_conv else None
check(conv_hop_key is not None and crm_hop_key is not None and conv_hop_key != crm_hop_key,
      f"a chave usada de fato (CONVERSAS_HOP_KEY={conv_hop_key!r}) difere da chave do hub do CRM "
      f"({crm_hop_key!r}) — os dois apps nao compartilham estado de sessao")

# a guarda precisa envolver os DOIS redirects para "/" deste arquivo (o
# automatico de validate() e o do submit do formulario de login), senao um
# login recem-feito reabre a mesma janela de loop.
redirect_count = html.count("window.location.href = '/';")
check(redirect_count == 2, f"login.html tem os 2 redirects esperados para \"/\" (achou {redirect_count})")
hop_mark_count = html.count("sessionStorage.setItem(CONVERSAS_HOP_KEY, '1');")
check(hop_mark_count == 2, f"os 2 redirects para \"/\" marcam a guarda antes de navegar (achou {hop_mark_count})")


# ============ 2. Comportamental — a segunda chegada nao redireciona ============
print("\nLogin loop guard (Conversas) — comportamento (Node, se disponivel)")

_HARNESS = r"""
const fs = require('fs');
const scen = JSON.parse(process.argv[1]);
let cleared = 0;
const nav = [];
const session = scen.hop ? { conversas_inbox_hop: '1' } : {};
const local = {};
const mk = o => ({
  getItem: k => (k in o ? o[k] : null),
  setItem: (k, v) => { o[k] = String(v); },
  removeItem: k => { delete o[k]; },
});
global.sessionStorage = mk(session);
global.localStorage = mk(local);
global.Auth = {
  clearAuth() { cleared++; },
};
global.window = { location: { origin: 'https://conversas.test' } };
Object.defineProperty(global.window.location, 'href', {
  get: () => '', set: v => { nav.push(v); },
});
global.fetch = () => Promise.resolve({ ok: scen.me === 200, status: scen.me });
const dummyEl = { addEventListener: () => {}, href: '' };
global.document = { getElementById: () => dummyEl };

const html = fs.readFileSync('conversas/templates/login.html', 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
if (scripts.length !== 1) {
  throw new Error('esperava exatamente 1 <script> inline em login.html, achou ' + scripts.length);
}
// eval() aqui roda dentro de um subprocesso Node descartavel, sobre o
// source do PROPRIO repo (conversas/templates/login.html) lido do disco —
// nao ha entrada de usuario nem rede envolvida. Mesmo idioma ja usado em
// tests/test_auth_session_consistency.py (_HARNESS) para testar
// comportamento de <script> sem subir um browser real.
eval(scripts[0][1]);

setTimeout(() => {
  console.log(JSON.stringify({ nav, cleared, hop: !!session.conversas_inbox_hop }));
}, 0);
"""


def _run(**scen):
    node = shutil.which("node")
    if node is None:
        return None
    p = subprocess.run(
        [node, "-e", _HARNESS, "--", json.dumps(scen)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    check(p.returncode == 0, f"harness node executa sem erro (stderr: {p.stderr.strip()[:400]})")
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)


r_first = _run(hop=False, me=200)
if r_first is None:
    print("  SKIP: node indisponivel — cobertura estatica acima ja garante a estrutura")
else:
    check(r_first["nav"] == ["/"], f"1a chegada com cookie valido navega para \"/\" (got {r_first['nav']})")
    check(r_first["hop"] is True, "1a chegada marca a guarda antes de navegar")
    check(r_first["cleared"] == 0, "1a chegada nao limpa a sessao do cliente")

r_second = _run(hop=True, me=200)
if r_second is not None:
    check(r_second["nav"] == [], f"2a chegada (guarda ja marcada) NAO redireciona de novo (got {r_second['nav']})")
    check(r_second["cleared"] == 1, "2a chegada limpa a sessao do cliente (Auth.clearAuth)")
    check(r_second["hop"] is False, "2a chegada remove a guarda (nao fica presa em '1' para sempre)")

r_unauth = _run(hop=False, me=401)
if r_unauth is not None:
    check(r_unauth["nav"] == [], f"sem cookie valido, nao ha redirecionamento (got {r_unauth['nav']})")
    check(r_unauth["cleared"] == 1, "sem cookie valido, a sessao do cliente e limpa")


# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DO LOGIN LOOP GUARD (CONVERSAS) PASSARAM")
