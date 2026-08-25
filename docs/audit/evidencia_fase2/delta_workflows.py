# -*- coding: utf-8 -*-
"""Delta funcional: snapshot antigo do repo x export ATUAL de producao."""
import io
import json
import os
import sys

REPO = sys.argv[1]
WF = sys.argv[2]

PARES = [
    ("WF-01 Agente Bia",
     f"{REPO}/n8n/workflows/live_exports/20260708_1443/WF-01_Agente_Bia.json",
     f"{WF}/wf01_agente_bia.json"),
    ("Agente Gerenciador de Leads",
     f"{REPO}/n8n/workflows/live_exports/20260708_1443/Agente_Gerenciador_de_Leads_BnA.json",
     f"{WF}/gerenciador_leads.json"),
    ("Formulario do Site",
     None,  # nao existia no snapshot
     f"{WF}/formulario_site.json"),
]


def resumo(caminho):
    d = json.load(io.open(caminho, encoding="utf-8"))
    nodes = {}
    for n in d.get("nodes", []):
        par = n.get("parameters", {}) or {}
        nodes[n.get("name")] = {
            "type": n.get("type", "").split(".")[-1],
            "url": par.get("url"),
            "method": (par.get("method") or "").upper(),
            "onError": n.get("onError"),
            "retryOnFail": n.get("retryOnFail"),
            "model": par.get("modelName"),
            "webhookPath": par.get("path"),
            "sysLen": len(str((par.get("options") or {}).get("systemMessage") or "")),
        }
    return d, nodes


for rotulo, velho, novo in PARES:
    print("=" * 78)
    print(rotulo)
    print("=" * 78)
    dn, nn = resumo(novo)
    if velho and os.path.exists(velho):
        dv, nv = resumo(velho)
        so_velho = sorted(set(nv) - set(nn))
        so_novo = sorted(set(nn) - set(nv))
        comuns = sorted(set(nn) & set(nv))
        print(f"  nodes: antes={len(nv)}  agora={len(nn)}")
        print(f"  REMOVIDOS ({len(so_velho)}):")
        for k in so_velho:
            print(f"     - {k}  [{nv[k]['type']}] {nv[k]['url'] or ''}")
        print(f"  ADICIONADOS ({len(so_novo)}):")
        for k in so_novo:
            print(f"     + {k}  [{nn[k]['type']}] {nn[k]['url'] or ''}")
        print("  ALTERADOS:")
        for k in comuns:
            difs = [c for c in ("url", "method", "onError", "retryOnFail",
                                "model", "webhookPath")
                    if nv[k].get(c) != nn[k].get(c)]
            if abs(nv[k]["sysLen"] - nn[k]["sysLen"]) > 50:
                difs.append(f"systemMessage {nv[k]['sysLen']}->{nn[k]['sysLen']} chars")
            if difs:
                print(f"     ~ {k}: ", end="")
                for c in difs:
                    if c.startswith("systemMessage"):
                        print(c, end="  ")
                    else:
                        print(f"{c}: {nv[k].get(c)!r} -> {nn[k].get(c)!r}", end="  ")
                print()
    else:
        print("  SEM SNAPSHOT ANTERIOR — workflow nunca foi auditado")
        print(f"  nodes agora: {len(nn)}")
        for k, v in nn.items():
            if v["url"] or v["webhookPath"]:
                print(f"     {k}: {v['method'] or 'GET'} {v['url'] or ('/webhook/'+str(v['webhookPath']))}")
    print()

# ── rastreio explicito do Notificador ───────────────────────────────────────
print("=" * 78)
print("RASTREIO DO NOTIFICADOR NOS EXPORTS ATUAIS")
print("=" * 78)
achou = False
for nome in sorted(os.listdir(WF)):
    if not nome.endswith(".json"):
        continue
    bruto = io.open(os.path.join(WF, nome), encoding="utf-8").read()
    d = json.loads(bruto)
    for termo in ("notificacao", "Notificador", "notificação", "lead_qualificado"):
        if termo.lower() in bruto.lower():
            achou = True
            for n in d.get("nodes", []):
                if termo.lower() in json.dumps(n, ensure_ascii=False).lower():
                    par = n.get("parameters", {}) or {}
                    print(f"  {d['name']}")
                    print(f"    node        : {n['name']}")
                    print(f"    type        : {n['type']}")
                    print(f"    method+url  : {(par.get('method') or 'GET')} {par.get('url')}")
                    print(f"    auth        : {par.get('authentication') or 'NENHUMA'}")
                    print(f"    credenciais : {list((n.get('credentials') or {}).keys()) or 'NENHUMA'}")
                    print(f"    ligado como : ", end="")
                    conns = d.get("connections", {}).get(n["name"], {})
                    print(list(conns.keys()) or "DESCONECTADO")
                    for tipo, saidas in conns.items():
                        for grupo in saidas:
                            for alvo in grupo:
                                print(f"                  {tipo} -> {alvo['node']}")
            break
if not achou:
    print("  nenhuma referencia")
