# -*- coding: utf-8 -*-
"""
AUDIT-2026-08 (F-029) — a UNICA autenticacao do webhook nunca era exercitada.

`POST /webhook` e o unico ponto de entrada do sistema alcancavel pela internet
sem sessao. O que o separa de qualquer pessoa na internet e uma linha:

    if META_APP_SECRET:
        if not _verify_meta_signature(raw_body, sig): -> 403

A auditoria contou 29 arquivos de teste que mencionam `META_APP_SECRET`. Todos
o definem como `""` — justamente para DESLIGAR a verificacao e poder postar
payloads a vontade. Ou seja: a suite inteira exercitava o webhook com o portao
aberto, e nenhum teste jamais verificou o portao em si. Se alguem invertesse a
condicao, trocasse `compare_digest` por `==`, comparasse o corpo ja parseado em
vez do cru, ou simplesmente apagasse o bloco, a suite continuaria verde.

Este arquivo cobre exatamente esse buraco, com o segredo LIGADO:

  1. sem assinatura            -> 403
  2. assinatura errada         -> 403
  3. assinatura de OUTRO corpo -> 403  (nao basta ser uma assinatura valida)
  4. assinatura correta        -> 200
  5. corpo REEMBALADO (mesmo JSON, espacos diferentes) -> 403
     Esta e a que garante que a verificacao usa o corpo CRU. Se alguem passar a
     assinar `json.dumps(await request.json())`, 1-4 continuam passando e esta
     falha.
  6. sem META_APP_SECRET fora de development -> nao pode aceitar (fail-closed)
  7. a comparacao e feita com hmac.compare_digest, nao com `==`

Rodar:  python tests/test_conversas_webhook_signature.py
"""
import hashlib
import hmac
import io
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_webhook_signature_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

SEGREDO = "segredo-de-app-da-meta-para-teste"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["N8N_AGENT_ENABLED"] = "false"
# O ponto do arquivo: o segredo fica LIGADO.
os.environ["META_APP_SECRET"] = SEGREDO

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, Base  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(main.app)

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


def assina(corpo: bytes, segredo: str = SEGREDO) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


# Payload minimo e INOFENSIVO: sem `messages`, o handler nao escreve nada. O que
# se mede aqui e o portao, nao o processamento.
CORPO = json.dumps({"entry": [{"changes": [{"value": {}}]}]}, separators=(",", ":")).encode()
H = {"Content-Type": "application/json"}


print("PORTAO — o webhook so aceita o que a Meta assinou")

r = client.post("/webhook", content=CORPO, headers=H)
check(r.status_code == 403, f"sem X-Hub-Signature-256 -> 403 (veio {r.status_code})")

r = client.post("/webhook", content=CORPO,
                headers={**H, "X-Hub-Signature-256": "sha256=" + "0" * 64})
check(r.status_code == 403, f"assinatura errada -> 403 (veio {r.status_code})")

r = client.post("/webhook", content=CORPO,
                headers={**H, "X-Hub-Signature-256": assina(b'{"outro":"corpo"}')})
check(r.status_code == 403,
      f"assinatura VALIDA de outro corpo -> 403 (veio {r.status_code})")

r = client.post("/webhook", content=CORPO,
                headers={**H, "X-Hub-Signature-256": assina(CORPO, "segredo-errado")})
check(r.status_code == 403,
      f"assinatura com o segredo errado -> 403 (veio {r.status_code})")

r = client.post("/webhook", content=CORPO,
                headers={**H, "X-Hub-Signature-256": assina(CORPO)})
check(r.status_code == 200, f"assinatura correta -> 200 (veio {r.status_code})")


print("\nCORPO CRU — a assinatura cobre os BYTES, nao o JSON")

# Mesmo objeto, serializacao diferente (espacos). Se a verificacao passar a
# reserializar o corpo antes de assinar, os checks acima continuam verdes e
# ESTE fica vermelho — que e exatamente o ponto dele.
CORPO_ESPACADO = json.dumps(json.loads(CORPO), separators=(", ", ": ")).encode()
check(CORPO_ESPACADO != CORPO and json.loads(CORPO_ESPACADO) == json.loads(CORPO),
      "o corpo reembalado e o MESMO JSON com bytes diferentes (premissa do teste)")

r = client.post("/webhook", content=CORPO_ESPACADO,
                headers={**H, "X-Hub-Signature-256": assina(CORPO)})
check(r.status_code == 403,
      f"corpo reembalado com a assinatura do original -> 403 (veio {r.status_code})")

r = client.post("/webhook", content=CORPO_ESPACADO,
                headers={**H, "X-Hub-Signature-256": assina(CORPO_ESPACADO)})
check(r.status_code == 200,
      f"corpo reembalado com a PROPRIA assinatura -> 200 (veio {r.status_code})")


print("\nFAIL-CLOSED — sem segredo, fora de development, nao passa nada")

fonte = (CONVERSAS_DIR / "app" / "routers" / "webhook.py").read_text(encoding="utf-8")
check("_is_signature_required" in fonte,
      "existe a decisao explicita de exigir assinatura fora de development")
# O bloco tem que REJEITAR quando nao ha segredo e a assinatura e exigida.
trecho = fonte.split("raw_body = await request.body()", 1)[1][:1400]
check("elif _is_signature_required():" in trecho and "raise HTTPException" in trecho,
      "sem META_APP_SECRET e com assinatura exigida, a rota LEVANTA (nao segue)")
check(trecho.index("if META_APP_SECRET:") < trecho.index("json.loads"),
      "a verificacao acontece ANTES de qualquer parsing do corpo")

import app.routers.webhook as wh  # noqa: E402

os.environ["ENVIRONMENT"] = "production"
try:
    exige = wh._is_signature_required()
finally:
    os.environ["ENVIRONMENT"] = "development"
check(exige is True,
      "fora de development a assinatura e obrigatoria (fail-closed, nao fail-open)")


print("\nCOMPARACAO — tempo constante")

vfonte = fonte
inicio = vfonte.find("def _verify_meta_signature")
corpo_fn = vfonte[inicio:inicio + 900] if inicio >= 0 else ""
check(inicio >= 0, "_verify_meta_signature existe neste modulo")
check("compare_digest" in corpo_fn,
      "a comparacao usa hmac.compare_digest (== vaza quanto do hash bateu)")
check("hashlib.sha256" in corpo_fn or "sha256" in corpo_fn,
      "o digest e sha256, como a Meta manda")

# Controle: se o slice acima pegasse a funcao errada, os dois checks passariam
# por acidente em outro codigo. Confere que e a funcao certa.
check("X-Hub-Signature" in fonte, "o modulo trata o header X-Hub-Signature-256")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: a unica autenticacao do webhook Meta esta coberta")
