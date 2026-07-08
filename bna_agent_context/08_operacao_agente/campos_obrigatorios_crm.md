---
context_id: "campos_obrigatorios_crm"
category: "operacao"
destination: "geral"
product: "geral"
risk_level: "high"
validity: "2026"
source: "live_bia_prompt"
status: "validado"
last_review: "2026-07-08"
---

# Campos do CRM (payload `enviar_ao_gerenciador`)

SEMPRE enviar TODOS os campos; sem informação → string vazia `""`. NUNCA
inventar dados.

| Campo | Conteúdo | Regra |
|---|---|---|
| whatsapp | nº do cliente | OBRIGATÓRIO SEMPRE (sem ele, não chamar a tool) |
| nome | nome do cliente | obrigatório p/ handoff |
| destinos | padronizados: "Atacama", "Santiago", "Uyuni" (múltiplos separados por vírgula) | obrigatório p/ handoff |
| data_chegada | YYYY-MM-DD | se souber |
| data_partida | YYYY-MM-DD | se souber |
| total_dias | ex.: "7" | quando não há datas exatas |
| email | email do cliente | obrigatório p/ handoff |
| num_viajantes | APENAS adultos | obrigatório p/ handoff |
| num_criancas | total de crianças; "0" se nenhuma | |
| idades_criancas | "6, 6, 3" | NÃO enviar se não há crianças |
| datas_destinos | JSON divisão por destino, ex.: {"Atacama":{"dias":"4"},"Uyuni":{"dias":"3"}} | se souber |
| contexto_conversa | resumo do que foi discutido | sempre útil |
| pronto_para_humano | "true" / "false" (string) | ver handoff |

## Os 4 OBRIGATÓRIOS para handoff (`pronto_para_humano = "true"`)

1. Nome completo
2. Destino(s)
3. Número de viajantes (adultos)
4. Email

Faltando qualquer um → NÃO fazer handoff; pedir gentilmente o que falta
("pra montar sua cotação, me passa seu email? 😊").

## Desejáveis (não bloqueiam)

Crianças (quantas/idades), datas ou total de dias, divisão entre destinos.
