# Índice de pendências — mapa por arquivo (área explícita de validação)

> Gerado em 2026-07-14 pelo pacote **N8N-BIA-GUARDRAILS-03** a partir de uma
> varredura real dos marcadores `[PENDENTE_VALIDACAO]` do vault. **Não** é
> conteúdo novo: apenas localiza, por arquivo, o que já está marcado como
> incerto. A lista temática (agrupada por assunto) continua em
> [pendencias_validacao.md](pendencias_validacao.md); este arquivo é a visão
> por arquivo, para o revisor saber exatamente onde abrir.

## Regra que este índice reforça

> Tudo listado abaixo é **dado NÃO confirmado**. Enquanto um item estiver
> marcado `[PENDENTE_VALIDACAO]`, a BIA **não** o trata como fato: ela escala
> para atendimento humano (ver `09_guardrails/nunca_inventar.md` e a regra de
> ouro em `../00_README.md`). Validar sempre na fonte real e seguir
> [checklist_atualizacao.md](checklist_atualizacao.md) para dar baixa.

## Contagem

- **134** ocorrências do token `[PENDENTE_VALIDACAO]` no total (contagem bruta
  do validator `scripts/validate_bna_agent_context.py`).
- **122** são itens de contexto reais, em **34** arquivos (ver tabelas abaixo).
- **12** são usos ilustrativos/documentais do próprio token, **não** são dados
  pendentes: 8 nos índices originais (`../00_README.md`, `schema_frontmatter.md`,
  `checklist_atualizacao.md`, `pendencias_validacao.md`) + 4 neste próprio
  arquivo, ao explicar a regra.

## Itens pendentes reais, por pasta (34 arquivos)

### 04_precos/ — 49 marcadores (prioridade máxima: preços)
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| precos_2026_atacama.md | 18 | pendente_validacao |
| precos_2026_santiago.md | 17 | pendente_validacao |
| precos_2026_uyuni.md | 7 | pendente_validacao |
| regras_de_preco.md | 1 | validado (marcador pontual) |

### 05_politicas/ — 20 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| cancelamento.md | 7 | pendente_validacao |
| pagamento.md | 6 | pendente_validacao |
| termos_e_condicoes.md | 5 | pendente_validacao |
| desconto_pix.md | 1 | pendente_validacao |
| lgpd_privacidade.md | 1 | pendente_validacao |

### 02_destinos/ — 18 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| atacama.md | 7 | validado (marcadores pontuais) |
| santiago.md | 6 | validado (marcadores pontuais) |
| uyuni.md | 2 | validado (marcadores pontuais) |
| logistica_geral.md | 2 | validado (marcadores pontuais) |
| melhor_epoca.md | 1 | validado (marcadores pontuais) |

### 07_faq_objecoes/ — 13 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| faq_clientes.md | 5 | pendente_validacao |
| objecoes_seguranca.md | 3 | pendente_validacao |
| objecoes_concorrencia.md | 2 | pendente_validacao |
| objecoes_preco.md | 2 | pendente_validacao |
| quando_escalar.md | 1 | validado (marcador pontual) |

### 06_saude_seguranca/ — 9 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| criancas.md | 3 | validado (marcadores pontuais) |
| altitude.md | 2 | pendente_validacao |
| emergencias.md | 2 | pendente_validacao |
| idosos.md | 1 | pendente_validacao |
| restricoes_e_cuidados.md | 1 | pendente_validacao |

### 03_tours/ — 9 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| atacama_especiais_vulcao_lascar_toco_trekking_bike_cascatas_cavalgadas.md | 4 | validado (marcadores pontuais) |
| santiago_neve.md | 3 | validado (marcadores pontuais) |
| atacama_astronomico.md | 1 | validado (marcador pontual) |
| santiago_vinicolas.md | 1 | validado (marcador pontual) |

### 01_empresa/ — 5 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| canais_atendimento.md | 2 | validado (marcadores pontuais) |
| empresa.md | 2 | validado (marcadores pontuais) |
| proposta_de_valor.md | 1 | validado (marcador pontual) |

### 08_operacao_agente/ — 2 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| handoff_humano.md | 2 | validado (marcadores pontuais) |

### 09_guardrails/ — 3 marcadores
| Arquivo | Marcadores | status frontmatter |
|---|---|---|
| dados_sensiveis.md | 1 | validado (marcador pontual) |
| nao_prometer_disponibilidade.md | 1 | validado (marcador pontual) |
| nunca_inventar.md | 1 | validado (marcador pontual) |

## Nota sobre `status: validado` + marcadores pontuais

Vários arquivos acima têm `status: "validado"` no frontmatter mas ainda
contêm marcadores `[PENDENTE_VALIDACAO]` isolados. Isso é o padrão previsto
pelo [schema_frontmatter.md](schema_frontmatter.md): `status` é do **arquivo**
como um todo; um marcador pontual sinaliza um dado específico ainda a
confirmar sem rebaixar o arquivo inteiro. O validator emite **AVISO** (não
falha) nesses casos. **Decisão do João (não automatizável):** para cada um,
confirmar se o marcador é realmente pontual ou se o arquivo deveria voltar a
`pendente_validacao`. Nada foi alterado automaticamente aqui — este índice
apenas os torna visíveis.
