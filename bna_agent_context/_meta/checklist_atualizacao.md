# Checklist de atualização (para o João)

## Regra nº 1

**NUNCA editar o prompt do workflow n8n direto para mudar preço/política.**
O fluxo é: editar o arquivo aqui → commit/PR → re-ingestão (quando o RAG
existir) → a BIA passa a responder com o valor novo. Enquanto o RAG não
existe, o vault é a fonte para a PRÓXIMA versão do prompt — mudanças urgentes
no prompt live devem ser espelhadas aqui NO MESMO DIA.

## Para atualizar um preço

1. Abrir o arquivo em `04_precos/` do destino.
2. Alterar o valor; remover o `[PENDENTE_VALIDACAO]` daquela linha.
3. Atualizar `last_review` no frontmatter; se não restam pendências no
   arquivo, mudar `status` para `"validado"`.
4. Remover o item correspondente de `_meta/pendencias_validacao.md` (e de
   `04_precos/pendencias_precos.md` se for o caso).
5. Commit com mensagem `docs(n8n): atualizar preco <tour> <ano>`.
6. (Com RAG ativo) rodar o workflow de ingestão e conferir no log que o
   chunk foi atualizado.

## Para validar uma pendência

1. Procurar o item em `_meta/pendencias_validacao.md`.
2. Confirmar a informação na fonte real (financeiro, operação, parceiro).
3. Editar o arquivo de contexto: corrigir/confirmar o texto, remover o
   marcador `[PENDENTE_VALIDACAO]`.
4. Atualizar frontmatter (`status`, `last_review`) e remover da lista de
   pendências.

## Para adicionar tour/política nova

1. Criar arquivo na pasta certa usando o schema
   (`_meta/schema_frontmatter.md`), com `context_id` único.
2. Preço SEMPRE em `04_precos/` (o arquivo do tour só referencia).
3. Adicionar linha em `_meta/mapa_de_arquivos.md`.
4. Commit + (futuro) re-ingestão.

## Revisão periódica sugerida

- Mensal: preços e disponibilidade sazonal.
- Trimestral: políticas, FAQ (alimentar com perguntas reais), exemplos.
- Anual (novembro): virada de catálogo/preços do ano seguinte — criar
  `precos_<ano>_*.md` novos e arquivar os antigos.
