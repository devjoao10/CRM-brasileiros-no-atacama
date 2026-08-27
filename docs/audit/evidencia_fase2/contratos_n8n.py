# -*- coding: utf-8 -*-
"""
Extrai TODA chamada HTTP dos tres workflows n8n ATUAIS e confronta com as rotas
que a aplicacao realmente expoe.

Evidencia, nao suposicao: as rotas do CRM/Conversas sao lidas do proprio codigo
(AST sobre os decoradores @router.<verbo>("<path>") mais o prefixo do APIRouter),
e cada chamada do n8n e casada contra elas convertendo os placeholders
`{coisa}` do n8n e os `{coisa}` do FastAPI para um curinga comum.

Sai com:
  - inventario de chamadas (workflow, node, metodo, URL, auth, corpo)
  - rota correspondente no codigo, ou AUSENTE
  - webhooks n8n expostos por cada workflow
"""
import ast
import io
import json
import os
import re
import sys

REPO = sys.argv[1]
WF_DIR = sys.argv[2]


# ── 1. rotas reais da aplicacao, por AST ────────────────────────────────────
def rotas_do_servico(raiz, pacote):
    rotas = []
    for base, _, arquivos in os.walk(os.path.join(raiz, pacote, "routers")):
        for nome in arquivos:
            if not nome.endswith(".py"):
                continue
            caminho = os.path.join(base, nome)
            try:
                arvore = ast.parse(io.open(caminho, encoding="utf-8").read())
            except SyntaxError:
                continue
            prefixo = ""
            for no in ast.walk(arvore):
                if (isinstance(no, ast.Assign) and isinstance(no.value, ast.Call)
                        and getattr(no.value.func, "id", "") == "APIRouter"):
                    for kw in no.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            prefixo = kw.value.value
            for no in ast.walk(arvore):
                if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in no.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    f = dec.func
                    if not (isinstance(f, ast.Attribute)
                            and getattr(f.value, "id", "") == "router"):
                        continue
                    verbo = f.attr.upper()
                    if verbo not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        continue
                    if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                        continue
                    caminho_rota = prefixo + dec.args[0].value
                    deps = []
                    for kw in dec.keywords:
                        if kw.arg == "dependencies":
                            deps.append("dependencies=")
                    guarda = []
                    for arg in list(no.args.args) + list(no.args.kwonlyargs):
                        pass
                    fonte = ast.get_source_segment(
                        io.open(caminho, encoding="utf-8").read(), no) or ""
                    for g in ("require_admin", "get_current_user", "verificar_api_key"):
                        if g in fonte.split("):")[0]:
                            guarda.append(g)
                    rotas.append({
                        "servico": pacote if raiz == REPO else "conversas",
                        "metodo": verbo,
                        "path": caminho_rota,
                        "func": no.name,
                        "arquivo": os.path.relpath(caminho, REPO).replace("\\", "/"),
                        "guardas": guarda,
                    })
    return rotas


rotas = rotas_do_servico(REPO, "app") + rotas_do_servico(
    os.path.join(REPO, "conversas"), "app")
for r in rotas:
    if r["arquivo"].startswith("conversas/"):
        r["servico"] = "conversas"
    else:
        r["servico"] = "crm"


def regex_de(path):
    """FastAPI '/api/leads/{id}' -> regex que casa o placeholder do n8n tambem."""
    p = re.escape(path)
    p = re.sub(r"\\\{[^}]*\\\}", r"[^/]+", p)
    return re.compile("^" + p + "$")


ROTAS_RX = [(regex_de(r["path"]), r) for r in rotas]


# ── 2. chamadas HTTP dos workflows ──────────────────────────────────────────
def analisar(caminho):
    d = json.load(io.open(caminho, encoding="utf-8"))
    saida = {"workflow": d.get("name"), "ativo": d.get("active"),
             "webhooks": [], "chamadas": [], "subworkflows": [], "modelos": []}
    for n in d.get("nodes", []):
        t = n.get("type", "")
        par = n.get("parameters", {}) or {}
        if t.endswith(".webhook"):
            saida["webhooks"].append({
                "node": n.get("name"), "metodo": par.get("httpMethod"),
                "path": par.get("path"),
                "responseMode": par.get("responseMode"),
                "auth": par.get("authentication") or "NENHUMA",
            })
        if "httpRequest" in t or t.endswith("toolHttpRequest"):
            saida["chamadas"].append({
                "node": n.get("name"), "tipo": t.split(".")[-1],
                "metodo": (par.get("method") or "GET").upper(),
                "url": par.get("url"),
                "auth": par.get("authentication") or "NENHUMA",
                "cred": list((n.get("credentials") or {}).keys()),
                "corpo": par.get("jsonBody") or par.get("parametersBody") or "",
                "neverError": "neverError" in json.dumps(par),
                "retry": n.get("retryOnFail", False),
                "onError": n.get("onError"),
            })
        if t.endswith("toolWorkflow"):
            wf = (par.get("workflowId") or {})
            saida["subworkflows"].append({
                "node": n.get("name"),
                "id": wf.get("value"), "nome": wf.get("cachedResultName")})
        if "lmChat" in t:
            saida["modelos"].append({"node": n.get("name"),
                                     "modelName": par.get("modelName")})
    return saida


print("=" * 78)
print(f"ROTAS REAIS NO CODIGO: {len(rotas)} "
      f"(crm={sum(1 for r in rotas if r['servico']=='crm')}, "
      f"conversas={sum(1 for r in rotas if r['servico']=='conversas')})")
print("=" * 78)

problemas = []
for nome_arq in sorted(os.listdir(WF_DIR)):
    if not nome_arq.endswith(".json"):
        continue
    w = analisar(os.path.join(WF_DIR, nome_arq))
    print(f"\n### {w['workflow']}   (ativo={w['ativo']})")
    for wh in w["webhooks"]:
        print(f"  WEBHOOK  {wh['metodo']:5} /webhook/{wh['path']:22} "
              f"auth={wh['auth']}  responseMode={wh['responseMode']}")
    for sw in w["subworkflows"]:
        print(f"  SUBWF    {sw['nome']}  (id={sw['id']})")
    for m in w["modelos"]:
        print(f"  MODELO   node={m['node']!r}  modelName={m['modelName']!r}")
    for c in w["chamadas"]:
        url = c["url"] or ""
        # normaliza expressao n8n para um path comparavel
        u = url
        u = re.sub(r"^=?\{\{.*?\}\}", "", u)
        m = re.search(r"https?://[^/]+(/[^\s'\"?}]*)", u)
        path = m.group(1) if m else u
        path = re.sub(r"\{\{.*?\}\}", "X", path)
        path = re.sub(r"'\s*\+\s*.*$", "", path)
        path = path.split("?")[0].rstrip("/") or "/"
        alvo = None
        for rx, r in ROTAS_RX:
            if rx.match(path) and r["metodo"] == c["metodo"]:
                alvo = r
                break
        externo = "n8n:5678" in (url or "")
        if alvo:
            st = f"OK  -> {alvo['arquivo']}::{alvo['func']} guardas={alvo['guardas'] or ['NENHUMA']}"
        elif externo:
            st = "CHAMADA A OUTRO WORKFLOW n8n"
        else:
            st = "!! SEM ROTA CORRESPONDENTE"
            problemas.append((w["workflow"], c["node"], c["metodo"], path))
        print(f"  CALL     {c['metodo']:6} {path:44} auth={c['auth']:22} {st}")
        if not c["cred"] and not externo:
            print(f"           ^^ SEM CREDENCIAL ANEXADA no node {c['node']!r}")

print("\n" + "=" * 78)
print(f"CHAMADAS SEM ROTA CORRESPONDENTE: {len(problemas)}")
for p in problemas:
    print("  ", p)
