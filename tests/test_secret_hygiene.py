"""
AUDIT-2026-08-W1E — guard de higiene de segredos e do contexto de build.

Nasceu de F1: uma API key `bna_` VIVA ficou versionada em docs/ apontada como o
valor do header X-API-Key do n8n. Qualquer `bna_...` valido autentica em TODAS
as rotas /api/* do CRM e do Conversas (mesma tabela `users`), entao um doc
versionado com o valor real e equivalente a publicar a senha do sistema.

Cobre tambem os vizinhos que deixaram o vazamento acontecer / se espalhar:
  F7  contexto de build levando banco de dev para dentro da imagem;
  F8  .gitignore cobrindo APENAS o nome exato `.env` (sem .env.bak, sem dumps);
  F2  conversas subindo com SECRET_KEY vazio (JWT forjavel nos dois servicos);
  F9  CRM pinado abaixo das correcoes de seguranca que o Conversas ja tinha.

Estatico puro: sem rede, sem banco. Roda standalone:
    python tests/test_secret_hygiene.py

REGRA: este arquivo NUNCA imprime o valor casado — apenas arquivo:linha.
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELF = pathlib.Path(__file__).resolve()

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    )


# ── 1. Nenhuma credencial viva em arquivo versionado ──────────────────────
# Cada padrao e ancorado no FORMATO de emissao da credencial, nao em heuristica
# de entropia: API key do CRM (app/auth.py::generate_api_key), user/system token
# da Meta, chave de API do Google e cabecalho de chave privada PEM.
PATTERNS = {
    "API key do CRM (bna_)": re.compile(r"bna_[A-Za-z0-9_-]{40,}"),
    "token da Meta (EAA)": re.compile(r"EAA[A-Za-z0-9]{40,}"),
    "chave de API Google (AIza)": re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    "chave privada PEM": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
}

print("AUDIT-2026-08-W1E — higiene de segredos")

tracked = [t for t in git("ls-files", "-z").stdout.split("\0") if t]
check(len(tracked) > 0, "git ls-files retornou a lista de arquivos versionados")

hits = []
for rel in tracked:
    path = ROOT / rel
    if path.resolve() == SELF or not path.is_file():
        continue  # o proprio guard carrega os padroes
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, rx in PATTERNS.items():
            if rx.search(line):
                # NUNCA imprimir o trecho casado — so a localizacao.
                hits.append(f"{rel}:{lineno} ({label})")

for h in hits:
    print(f"    >>> credencial viva em {h}")
check(not hits, f"nenhuma credencial viva em arquivo versionado ({len(hits)} ocorrencia(s))")


# ── 2. .gitignore cobre variantes de .env, dumps e backups (F8) ───────────
def ignored(rel):
    return git("check-ignore", "-q", rel).returncode == 0


for rel in (".env.bak", ".env.production", "backups/x.sql", "dump.sql.gz"):
    check(ignored(rel), f".gitignore ignora {rel}")
check(not ignored(".env.example"), ".gitignore NAO ignora .env.example (template versionado)")


# ── 3. Contexto de build sem banco local nem .env (F7/F8) ─────────────────
for rel in (".dockerignore", "conversas/.dockerignore"):
    lines = {ln.strip() for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines()}
    check("*.db" in lines, f"{rel} exclui *.db")
    check(".env*" in lines, f"{rel} exclui .env*")


# ── 4. SECRET_KEY obrigatorio nos DOIS servicos do compose (F2) ───────────
COMPOSE_PATH = ROOT / "docker-compose.yml"
COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")


def secret_key_entries(service):
    """Linhas `SECRET_KEY=...` do bloco environment do servico."""
    try:
        import yaml
    except ImportError:
        # Fallback textual: recorta o bloco do servico ate o proximo servico
        # (indentacao de 2 espacos) — mesmo recorte usado pelo guard de infra.
        m = re.search(rf"\n  {service}:\n(.*?)(?=\n  \S|\Z)", COMPOSE_TEXT, re.S)
        block = m.group(1) if m else ""
        return re.findall(r"^\s*-\s*(SECRET_KEY=.*)$", block, re.M)
    env = yaml.safe_load(COMPOSE_TEXT)["services"][service]["environment"]
    items = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
    return [str(e) for e in items if str(e).startswith("SECRET_KEY=")]


for service in ("crm", "conversas"):
    entries = secret_key_entries(service)
    # `:?` faz o `docker compose up` abortar com mensagem legivel. Sem ele, a
    # interpolacao devolve string vazia e o servico cai no default do codigo.
    check(len(entries) == 1 and ":?" in entries[0],
          f"servico {service}: SECRET_KEY usa a forma ${{SECRET_KEY:?...}} (fail-closed)")


# ── 5. CRM nao pode ficar atras do Conversas nos pacotes compartilhados (F9)
def pins(path):
    out = {}
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        out[re.sub(r"\[.*\]", "", name).strip().lower()] = version.strip()
    return out


def as_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


crm_pins, conv_pins = pins("requirements.txt"), pins("conversas/requirements.txt")
for pkg in ("python-jose", "python-multipart", "jinja2"):
    a, b = crm_pins.get(pkg), conv_pins.get(pkg)
    check(a is not None and b is not None and as_tuple(a) >= as_tuple(b),
          f"{pkg}: pin do CRM ({a}) >= pin do Conversas ({b})")


if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nHIGIENE DE SEGREDOS OK")
