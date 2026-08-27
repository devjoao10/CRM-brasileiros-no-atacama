# -*- coding: utf-8 -*-
"""
Roda os PAYLOADS EXATOS que os tres workflows atuais enviam contra os schemas
Pydantic reais do CRM, na arvore ja estabilizada.

Isto e o teste de contrato que importa: nao "a rota existe", e sim "o corpo que
o n8n manda hoje e aceito pelo codigo de hoje". Os placeholders `{campo}` do
n8n sao substituidos pelos valores que os proprios toolDescriptions mandam
enviar quando o dado nao foi coletado — na maioria, string vazia.
"""
import io
import json
import os
import sys

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SEED_INITIAL_ADMIN", "false")
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")

import app.main  # noqa: E402  (registra todos os mappers)
import pydantic  # noqa: E402

from app.schemas.lead import LeadCreate, LeadUpdate  # noqa: E402
from app.schemas.task import TaskCreate  # noqa: E402
from app.schemas.pipeline import StageSchema  # noqa: E402

falhas = []


def tenta(rotulo, modelo, payload, esperado="aceitar"):
    try:
        obj = modelo(**payload)
        ok = (esperado == "aceitar")
        detalhe = {k: getattr(obj, k, None) for k in payload if hasattr(obj, k)}
        print(f"  {'PASS' if ok else 'FAIL'}: {rotulo} -> ACEITO")
        print(f"        resultado: {json.dumps(detalhe, default=str, ensure_ascii=False)[:220]}")
        if not ok:
            falhas.append(rotulo)
        return obj
    except pydantic.ValidationError as e:
        ok = (esperado == "recusar")
        print(f"  {'PASS' if ok else 'FAIL'}: {rotulo} -> RECUSADO")
        for err in e.errors()[:4]:
            print(f"        {'.'.join(str(x) for x in err['loc'])}: {err['msg']}")
        if not ok:
            falhas.append(rotulo)
        return None


print("=" * 78)
print("A) Gerenciador :: Tool Criar Lead  -> POST /api/leads")
print("=" * 78)
# Caso 1: lead com TODOS os dados coletados (o caminho feliz)
tenta("criar lead completo", LeadCreate, {
    "nome": "Joao Teste", "whatsapp": "5548988711776", "destinos": "Atacama, Uyuni",
    "email": "joao@example.com", "num_viajantes": "2", "num_criancas": "1",
    "idades_criancas": "6", "data_chegada": "2026-09-10",
    "data_partida": "2026-09-17", "total_dias": "7",
    "datas_destinos": {}, "dias_por_destino": {},
})
# Caso 2: o que a tool manda quando o dado NAO foi coletado — string vazia,
# exatamente como o toolDescription instrui ("envie vazio \"\"").
tenta("criar lead com campos vazios (instrucao literal da tool)", LeadCreate, {
    "nome": "Maria Teste", "whatsapp": "5548988711777", "destinos": "Atacama",
    "email": "", "num_viajantes": "", "num_criancas": "0",
    "idades_criancas": "", "data_chegada": "", "data_partida": "",
    "total_dias": "", "datas_destinos": {}, "dias_por_destino": {},
})
# Caso 3: o LLM deixa o placeholder sem substituir (acontece)
tenta("criar lead com placeholder nao substituido", LeadCreate, {
    "nome": "{nome}", "whatsapp": "{whatsapp}", "destinos": "{destinos}",
    "email": "{email}", "num_viajantes": "{num_viajantes}",
    "num_criancas": "{num_criancas}", "idades_criancas": "{idades_criancas}",
    "data_chegada": "{data_chegada}", "data_partida": "{data_partida}",
    "total_dias": "{total_dias}", "datas_destinos": {}, "dias_por_destino": {},
}, esperado="recusar")

print()
print("=" * 78)
print("B) Formulario do Site :: POST /api/leads  (numeros como NUMERO)")
print("=" * 78)
tenta("formulario, payload real", LeadCreate, {
    "nome": "Cliente Site", "whatsapp": "5548988711778",
    "destinos": "Atacama, Uyuni", "email": "site@example.com",
    "num_viajantes": 2, "num_criancas": 0,
    "data_chegada": "2026-10-01", "data_partida": "2026-10-08",
    "datas_destinos": {}, "dias_por_destino": {},
})

print()
print("=" * 78)
print("C) Gerenciador :: Tool Atualizar Lead -> PUT /api/leads/{id}")
print("=" * 78)
tenta("atualizar com todos os campos vazios", LeadUpdate, {
    "nome": "", "whatsapp": "", "destinos": "", "email": "",
    "num_viajantes": "", "num_criancas": "", "idades_criancas": "",
    "data_chegada": "", "data_partida": "", "total_dias": "",
    "datas_destinos": {}, "dias_por_destino": {},
})

print()
print("=" * 78)
print("D) Gerenciador :: Tool Criar Tarefa -> POST /api/tasks (lead_id STRING)")
print("=" * 78)
tenta("criar tarefa com lead_id string", TaskCreate, {
    "titulo": "Follow-up", "descricao": "Lead qualificado pela Bia",
    "lead_id": "42", "tipo": "automatica",
    "data_vencimento": "2026-09-01T10:00:00",
})
tenta("criar tarefa com data_vencimento vazia", TaskCreate, {
    "titulo": "Follow-up", "descricao": "x", "lead_id": "42",
    "tipo": "automatica", "data_vencimento": "",
}, esperado="recusar")

print()
print("=" * 78)
print("E) etapa_id que os workflows usam de fato")
print("=" * 78)
for etapa in ("nova_oportunidade", "sem_contato", "Sem Contato", "{etapa_id}"):
    tenta(f"StageSchema.id = {etapa!r}", StageSchema,
          {"id": etapa, "nome": "x"},
          esperado="aceitar" if etapa in ("nova_oportunidade", "sem_contato") else "recusar")

print()
print("=" * 78)
print(f"{len(falhas)} divergencia(s) de contrato" if falhas else "contratos conferem")
for f in falhas:
    print("  !!", f)
