"""
QA-CONV-01 / SEC-CONV-01 — Testes estaticos de seguranca do frontend Conversas.

Verifica, por inspecao do conversas.js, que a renderizacao e XSS-safe:
  - escapeHtml escapa aspas simples e duplas (protege contexto de atributo)
  - o src da <img> de media usa escapeHtml(msg.media_url) (media_url vem do webhook)
  - nao ha interpolacao crua de msg.media_url dentro de um atributo src

Rapido e sem boot de app. Roda standalone:  python tests/test_conversas_security.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

# AUDIT-2026-08-W0 — este binding NAO e cosmetico: e o discriminador de job do CI.
# .github/workflows/test.yml separa as duas suites com
# `grep -L/-l CONVERSAS_DIR tests/test_*.py`, e este arquivo era o UNICO teste do
# Conversas sem o literal — logo rodava no job do CRM (Python 3.11 +
# requirements.txt). Passava por acidente, porque nao importa `app.*`. No dia em
# que alguem transformar estes greps em teste de comportamento (que e o proximo
# passo obvio), o import ou falha por dependencia ausente ou, pior, resolve `app`
# para o pacote do CRM e afirma sobre o codigo errado.
CONVERSAS_DIR = ROOT / "conversas"
JS = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


print("SEC-CONV-01 — conversas.js XSS hardening")

# 1. escapeHtml precisa escapar aspas (senao quebra atributos)
check(
    "replace(/\"/g, '&quot;')" in JS and "replace(/'/g, '&#39;')" in JS,
    "escapeHtml escapa aspas duplas e simples",
)

# 2. media_url deve ir escapado para o src da img
check(
    'src="${escapeHtml(msg.media_url)}"' in JS,
    "img de media usa escapeHtml(msg.media_url) no src",
)

# 3. NAO pode existir media_url cru dentro de src="${...}"
raw_media = re.search(r'src="\$\{\s*msg\.media_url\s*\}"', JS)
check(raw_media is None, "nao ha msg.media_url cru interpolado em src")

# 4. Nenhum src="${...}" com interpolacao que nao passe por escapeHtml
for m in re.finditer(r'src="\$\{([^}]*)\}"', JS):
    expr = m.group(1)
    check(
        "escapeHtml(" in expr,
        f"src interpolado passa por escapeHtml (expr: {expr.strip()})",
    )

if failures:
    print(f"\n{len(failures)} FALHA(S) DE SEGURANCA")
    sys.exit(1)
print("\nTODOS OS CHECKS DE SEGURANCA PASSARAM")
