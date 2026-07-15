# 04_precos/ — Preços 2026 e regras de comunicação de preço

**Única pasta onde valores de preço podem existir.** Todo o resto do vault
referencia estes arquivos em vez de copiar números (evita divergência).

| Arquivo | Conteúdo | status |
|---|---|---|
| [precos_2026_atacama.md](precos_2026_atacama.md) | Tabelas de preço 2026 — Atacama | pendente_validacao |
| [precos_2026_santiago.md](precos_2026_santiago.md) | Tabelas de preço 2026 — Santiago | pendente_validacao |
| [precos_2026_uyuni.md](precos_2026_uyuni.md) | Tabelas de preço 2026 — Uyuni | pendente_validacao |
| [regras_de_preco.md](regras_de_preco.md) | Como comunicar preço (inviolável) | validado (marcador pontual) |
| [pendencias_precos.md](pendencias_precos.md) | Lacunas específicas de preço | pendente_validacao |

## Estado crítico

As **3 tabelas de preço estão inteiras `pendente_validacao`** (49 marcadores no
total desta pasta — a maior concentração do vault). Enquanto um preço estiver
marcado, a BIA **não informa** aquele valor: escala para humano
(ver `../09_guardrails/nunca_inventar.md` e `regras_de_preco.md`).

Pendências detalhadas: [../_meta/pendencias_index.md](../_meta/pendencias_index.md)
e a seção temática em [../_meta/pendencias_validacao.md](../_meta/pendencias_validacao.md).
Índice: [../00_README.md](../00_README.md).
