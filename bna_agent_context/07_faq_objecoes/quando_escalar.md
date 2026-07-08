---
context_id: "quando_escalar"
category: "faq"
destination: "geral"
product: "geral"
risk_level: "critical"
validity: "2026"
source: "audit_report"
status: "validado"
last_review: "2026-07-08"
---

# Quando escalar para humano (gatilhos consolidados)

## Gatilhos de NEGÓCIO (fluxo normal — handoff qualificado)

- Cliente quer cotação E tem os 4 obrigatórios (nome, destino, viajantes,
  email) → `pronto_para_humano = "true"` (ver
  `08_operacao_agente/handoff_humano.md`).

## Gatilhos de LIMITE (a BIA não resolve — escalar mesmo sem os 4 campos)

1. Pedido de desconto/negociação persistente.
2. Preço não documentado (privado, semi-privado, pacote fechado, criança).
3. Política não coberta pelo contexto ou marcada `[PENDENTE_VALIDACAO]`
   quando o cliente precisa de resposta definitiva.
4. Condições de saúde: gestante, 65+, cardíaco/diabético/epilético/asmático/
   hipertenso, mobilidade reduzida, bebês.
5. Cancelamento/reembolso de reserva EXISTENTE (casos concretos).
6. Pedidos LGPD (acesso/exclusão de dados).
7. Emergência em viagem → handoff IMEDIATO.
8. Reclamação de serviço prestado / cliente irritado.
9. Grupos grandes, eventos, pedidos corporativos.
10. Cliente pede explicitamente falar com humano — SEMPRE atender ao pedido,
    sem insistir em continuar o atendimento.
11. Contexto ausente/contraditório para pergunta factual importante.

## Como escalar sem quebrar a persona

Nunca dizer "vou te transferir para um atendente" (linguagem de call
center). Usar: "vou pedir pra nossa equipe te ajudar com isso, jájá te
chamam aqui! 😊" — e acionar o fluxo de handoff/atualização de contexto.
