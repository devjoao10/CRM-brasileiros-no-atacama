# BNA Agent Context Vault — Contexto versionado da BIA

Este diretório é a **fonte única de verdade** do conhecimento de negócio que a
BIA (agente de atendimento WhatsApp da Brasileiros no Atacama) usa para
atender clientes. Ele substitui, gradualmente, o conhecimento hoje embutido no
system prompt do workflow n8n `WF-01 Agente Bia`.

## Por que existe

- O prompt de produção da BIA embute catálogo, preços e políticas (~20k chars).
  Mudar um preço = editar workflow de produção. Este vault tira o conhecimento
  do prompt e o coloca em arquivos versionados, revisáveis por PR.
- Conteúdo importante (LGPD, pagamento, saúde/altitude) foi perdido em edições
  do prompt. Aqui nada se perde: o Git guarda o histórico.

## Como será usado (RAG futuro)

1. `N8N-RAG-INGEST-01`: um workflow n8n lê estes arquivos, gera chunks +
   embeddings e grava no pgvector (Postgres existente).
2. `N8N-BIA-CONTEXT-TOOL-01`: a BIA ganha a tool **"Buscar Contexto BNA"** —
   antes de responder qualquer pergunta factual/comercial/de política, ela
   busca aqui os trechos relevantes (top 4–6 chunks) e responde com base neles.
3. O system prompt da BIA encolhe para ≤ 6–8k chars (ver
   `_meta/system_prompt_futuro_curto.md`).

## O que fica no system prompt vs. o que é recuperado daqui

| Fica no prompt | Vem do vault (RAG) |
|---|---|
| Papel/persona e tom de voz (resumo) | Catálogo completo de tours |
| Formato de resposta WhatsApp (`\|\|\|`) | Preços e condições |
| Regras de uso das tools | Políticas (pagamento, cancelamento, LGPD) |
| Regras de handoff | Saúde/altitude/restrições detalhadas |
| Guardrails (nunca inventar etc.) | FAQ e tratamento de objeções |
| 3–5 exemplos curtos de diálogo | Logística, melhor época, dicas |

## Ordem de prioridade em conflito

**guardrails > políticas > preços > produtos > FAQ/objeções > tom/exemplos**

Se dois arquivos conflitarem, vale o de categoria mais prioritária. Se um
arquivo conflitar consigo mesmo ou estiver marcado `[PENDENTE_VALIDACAO]`,
a BIA NÃO usa o dado — escala para humano.

## Regra de ouro

> **Se o contexto não existe, está incompleto ou está marcado
> `[PENDENTE_VALIDACAO]`, a BIA NÃO inventa: ela escala para atendimento
> humano.** Preço sem fonte = não informar. Política sem fonte = não prometer.

## Estrutura

```
00_persona/          quem é a BIA, tom, exemplos, proibições de linguagem
01_empresa/          o que é a BnA, proposta de valor, canais
02_destinos/         Atacama, Santiago, Uyuni, melhor época, logística
03_tours/            um arquivo por tour/grupo de tours
04_precos/           preços 2026 por destino + regras + pendências
05_politicas/        pagamento, Pix, cancelamento, termos, LGPD
06_saude_seguranca/  altitude, crianças, idosos, restrições, emergências
07_faq_objecoes/     FAQ e objeções (estrutura inicial)
08_operacao_agente/  fluxo, campos CRM, handoff, tools, formato WhatsApp
09_guardrails/       regras invioláveis
_meta/               schema, mapa, checklist, pendências, prompt futuro
```

## Navegação

- Cada pasta de conteúdo (`00_persona/` … `09_guardrails/`) tem um `README.md`
  que lista seus arquivos, o arquivo canônico da pasta e o total de pendências.
- Pendências: visão por tema em `_meta/pendencias_validacao.md`; visão por
  arquivo (onde abrir) em `_meta/pendencias_index.md`.
- Mapa completo de arquivos: `_meta/mapa_de_arquivos.md`.

## Convenções

- Todo arquivo de contexto tem frontmatter YAML (ver
  `_meta/schema_frontmatter.md`).
- Dados incertos/antigos/recuperados de export: `[PENDENTE_VALIDACAO]` +
  `status: "pendente_validacao"` no frontmatter, e entrada em
  `_meta/pendencias_validacao.md`.
- NUNCA colocar aqui: segredos, credenciais, tokens, paths de webhook, dados
  pessoais de clientes (nomes, telefones, mensagens), e-mails privados da
  equipe. Contatos PÚBLICOS da empresa são permitidos.
- Fontes: `live_bia_prompt` (prompt de produção lido em 2026-07-08),
  `old_export` (export de 21/05/2026 — conteúdo removido do live),
  `manual_reconstruction` (estrutura criada agora, a preencher),
  `audit_report` (relatórios SDD).
