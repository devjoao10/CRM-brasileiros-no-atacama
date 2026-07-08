# Schema do frontmatter (obrigatório em todo arquivo de contexto)

Arquivos de contexto (tudo exceto `00_README.md` e `_meta/*`) começam com:

```yaml
---
context_id: "unique_id"          # único no vault, snake_case
category: "persona|empresa|destino|tour|preco|politica|saude|faq|operacao|guardrail"
destination: "atacama|santiago|uyuni|geral"
product: "tour_name_or_geral"    # snake_case
risk_level: "low|medium|high|critical"
validity: "2026"                 # ano de validade do conteúdo
source: "live_bia_prompt|old_export|manual_reconstruction|audit_report"
status: "validado|pendente_validacao"
last_review: "YYYY-MM-DD"
---
```

## Semântica dos campos

- **context_id**: chave estável para rastrear chunks no RAG e logs ("qual
  contexto a BIA usou nesta resposta?").
- **category/destination/product**: viram metadata de filtro na busca
  vetorial (ex.: pergunta sobre Uyuni → filtrar destination in (uyuni, geral)).
- **risk_level**: `critical` = erro aqui causa dano real (preço, política,
  saúde). Ingestão pode priorizar revisão por este campo.
- **source**:
  - `live_bia_prompt` — extraído do prompt de produção (leitura 2026-07-08);
  - `old_export` — recuperado do export de 21/05/2026 (conteúdo REMOVIDO do
    live; sempre nasce `pendente_validacao`);
  - `manual_reconstruction` — estrutura/nova redação criada no pacote
    N8N-CONTEXT-VAULT-01;
  - `audit_report` — consolidação dos relatórios SDD.
- **status**: `pendente_validacao` enquanto houver QUALQUER
  `[PENDENTE_VALIDACAO]` relevante no corpo; João muda para `validado` ao
  revisar (e atualiza `last_review`).

## Regras de corpo

- Marcar item incerto INLINE com `[PENDENTE_VALIDACAO]` + citar a fonte.
- Nunca incluir: segredos, credenciais, paths de webhook, dados pessoais de
  clientes, contatos privados da equipe.
- Preço: SÓ nos arquivos de `04_precos/` (outros arquivos referenciam) —
  evita divergência entre cópias.
