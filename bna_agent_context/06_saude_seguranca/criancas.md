---
context_id: "saude_criancas"
category: "saude"
destination: "geral"
product: "geral"
risk_level: "critical"
validity: "2026"
source: "live_bia_prompt"
status: "validado"
last_review: "2026-07-08"
---

# Crianças

## Regras vigentes (prompt LIVE)

- **Menores de 7 anos: não podem fazer tours acima de 4.000m.** (export
  acrescentava: tour privativo obrigatório para participar
  `[PENDENTE_VALIDACAO]`). Avisar com naturalidade e sugerir alternativas.
- **Menores de 12 anos: não recomendados para Uyuni.** Avisar se o destino
  incluir Uyuni.

## Como coletar composição da família (fluxo da conversa)

- Ao saber o nº de viajantes, perguntar: "são todos adultos ou tem crianças
  junto?"
- Se tiver crianças: quantas e idade de cada uma. Aceitar QUALQUER formato
  ("2 de 6 e 1 de 3", "gêmeos de 4", "filha de 8").
- Registro no CRM: `num_viajantes` = APENAS adultos; `num_criancas` = total;
  `idades_criancas` = idades separadas por vírgula. Sem crianças:
  `num_criancas = "0"` e NÃO enviar idades.

## Alternativas amigáveis para família (baixa altitude)

Valle de la Luna, Vallecito, Laguna Cejar, Baltinache, Tour Astronômico,
Cavalgadas — sempre conferir idade mínima específica se houver dúvida.

## Pendências

- Preço de criança (inteira/meia/isento) — NÃO documentado
  `[PENDENTE_VALIDACAO]` (ver `04_precos/pendencias_precos.md`).
- Idade mínima por tour individual (além das regras gerais) —
  `[PENDENTE_VALIDACAO]`.
