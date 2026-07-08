# Mapa de arquivos do vault

| Pasta | Arquivo | Conteúdo | Fonte principal |
|---|---|---|---|
| raiz | 00_README.md | propósito, uso RAG, prioridades | — |
| 00_persona | persona_bia.md | quem é a BIA | live |
| 00_persona | tom_de_voz.md | regras de estilo | live |
| 00_persona | exemplos_dialogo.md | few-shot padrão-ouro | live |
| 00_persona | proibicoes_de_linguagem.md | anti-robótico inviolável | live |
| 01_empresa | empresa.md | o que é a BnA, CNPJ | live + export |
| 01_empresa | proposta_de_valor.md | diferenciais | live |
| 01_empresa | canais_atendimento.md | contatos públicos | live + export |
| 02_destinos | atacama.md / santiago.md / uyuni.md | destino, logística, dicas | live + export |
| 02_destinos | melhor_epoca.md | sazonalidade | live + export |
| 02_destinos | logistica_geral.md | o que a BnA não faz, pós-compra | live + export |
| 03_tours | atacama_*.md (11) | um por tour/grupo | live |
| 03_tours | santiago_vinicolas.md / santiago_neve.md | tours Santiago | live + export |
| 03_tours | uyuni_expedicoes.md | formatos de expedição | live |
| 04_precos | precos_2026_*.md (3) | tabelas por destino | live (tudo pendente) |
| 04_precos | regras_de_preco.md | como comunicar preço | live |
| 04_precos | pendencias_precos.md | lacunas de preço | audit |
| 05_politicas | pagamento.md / desconto_pix.md | pagamento | live + export |
| 05_politicas | cancelamento.md | regras 85/50/0 + casos | live + export |
| 05_politicas | termos_e_condicoes.md | T&C resumo operacional | export |
| 05_politicas | lgpd_privacidade.md | LGPD (recuperado) | export |
| 06_saude_seguranca | altitude.md / criancas.md / idosos.md | restrições | live + export |
| 06_saude_seguranca | restricoes_e_cuidados.md | tabela consolidada | ambos |
| 06_saude_seguranca | emergencias.md | protocolo de emergência | export + audit |
| 07_faq_objecoes | faq_clientes.md | FAQ inicial | reconstrução |
| 07_faq_objecoes | objecoes_preco.md / objecoes_seguranca.md / objecoes_concorrencia.md | objeções | reconstrução |
| 07_faq_objecoes | quando_escalar.md | gatilhos de escalação | audit |
| 08_operacao_agente | fluxo_atendimento_bia.md | fluxo ponta a ponta | live |
| 08_operacao_agente | campos_obrigatorios_crm.md | payload gerenciador | live |
| 08_operacao_agente | handoff_humano.md | handoff único + validação | live + export |
| 08_operacao_agente | uso_de_tools.md | tools atuais + futura | live |
| 08_operacao_agente | formato_resposta_whatsapp.md | regra do \|\|\| | live |
| 09_guardrails | 5 arquivos | regras invioláveis | live + audit |
| _meta | schema_frontmatter.md | este schema | — |
| _meta | mapa_de_arquivos.md | este mapa | — |
| _meta | checklist_atualizacao.md | como João atualiza | — |
| _meta | pendencias_validacao.md | TODAS as pendências | — |
| _meta | system_prompt_futuro_curto.md | esqueleto do prompt ≤6-8k | — |

Nota de desvio: `objecoes_seguranca.md` (ASCII) em vez de
`objecoes_segurança.md` — nomes de arquivo sem acento para não quebrar
pipelines de ingestão.
