# Perpétua — Geração de PDF (ferramenta `generate_pdf_document`)

**Pacote:** PERPETUA-PDF-FIX-01 · **Tipo:** BUGFIX · **Status:** corrigido localmente, testado localmente, **não deployado**.
**Auditoria de origem:** PERPETUA-PDF-AUDIT-01 (causa raiz confirmada por reprodução hermética local).

## O que a ferramenta faz

`generate_pdf_document(filename, title, content)` em `app/services/ai_tools.py` é a
tool que a Perpétua (Gemini) chama quando o usuário pede um documento em PDF.
Pipeline completo:

```
POST /api/ai/chat (app/routers/ai.py)
  → tool call do Gemini → generate_pdf_document (fpdf2)
  → grava uploads/<nome>_<uuid6>.pdf
  → retorna {"success": true, "download_url": "/api/ai/download/<arquivo>"}
  → GET /api/ai/download/{filename} (autenticado, anti-traversal, valida %PDF-)
```

Formato aceito em `content`: `\n` quebra linha, `## ` inicia subtítulo, `- ` inicia
bullet; todo o resto é parágrafo. Não há tabelas reais nem Markdown completo;
caracteres fora de latin-1 (ex.: emoji) viram `?` (comportamento preservado).

## Bug corrigido

**Sintoma:** qualquer documento com duas linhas não vazias consecutivas falhava com
`Not enough horizontal space to render a single character` (fpdf2,
`fpdf/line_break.py`), e a mensagem interna bruta era devolvida ao usuário no chat.

**Causa raiz:** no fpdf2 moderno (≥2.5.2; requirements usa `fpdf2>=2.8.0`), o
default de `multi_cell` é `new_x=XPos.RIGHT` — após renderizar uma linha o cursor X
fica na borda direita (~200mm). A chamada seguinte `multi_cell(0, ...)` calcula
largura disponível ≈ 0 e nem um caractere cabe. Linhas em branco (`pdf.ln`) e
subtítulos (`cell(..., ln=1)`) resetavam o X por acaso, por isso documentos
"simples" às vezes funcionavam.

**Correção (mínima):**

1. `from fpdf.enums import XPos, YPos` e `new_x=XPos.LMARGIN, new_y=YPos.NEXT`
   nas duas chamadas `multi_cell` (parágrafos e bullets) — o cursor volta à margem
   esquerda e desce uma linha, via API oficial do fpdf2.
2. O bloco `except` deixou de devolver `str(e)`: o stack trace completo continua
   no log interno (`logger.exception("Erro ao gerar PDF")`), e o usuário/LLM recebe
   a mensagem estável: *"Não foi possível gerar o PDF. Tente novamente ou solicite
   o conteúdo como texto no chat."* — sem nome de biblioteca, caminho ou conteúdo.

## Contratos — nada mudou

Assinatura da função, nomes de argumentos, formato de retorno de sucesso
(`success`/`filename`/`download_url`/`message`), rotas `POST /api/ai/chat` e
`GET /api/ai/download/{filename}`, autenticação, Excel, banco, modelos, schemas e
migrations: **inalterados**. O retorno de erro continua `{"error": "..."}` (só o
texto ficou estável e seguro).

## Impacto no n8n

Nenhum. Confirmado na auditoria em duas fontes: exports versionados
(`n8n/workflows/`, `live_exports/20260708_1443/`) e inspeção LIVE via MCP
(2026-07-12) — nenhum dos 5 workflows ativos chama `/api/ai/*`.

## Testes

`tests/test_perpetua_pdf_generation.py` — hermético (sem Gemini/rede/banco real/
dados reais; PDFs em `tempfile.TemporaryDirectory`). 16 casos: regressão de duas
linhas consecutivas (falhava antes, passa agora — verificado nos dois estados),
parágrafos/listas/subtítulo consecutivos, CRLF, palavra de 800 chars, URL longa,
e-mail longo, UUID, JSON em linha única, tabela Markdown como texto, acentos,
emoji, vazio, 300 linhas, e falha interna sem vazamento de detalhe.

Rodar: `python tests/test_perpetua_pdf_generation.py` (processo próprio, como as
demais suítes).

## Rollback

`git revert` do commit do fix (1 arquivo funcional, ~12 linhas). Sem migração,
sem mudança de contrato, sem passo de infra.

## Pendências deixadas para pacotes futuros

- **DOCUMENT-FILENAME-SECURITY-01** (backlog registrado no vault): sanitização de
  `filename` na escrita de PDF **e** Excel (`os.path.basename`/whitelist — hoje um
  `filename` com `../` escapa de `uploads/` na gravação); revisão de
  propriedade/autorização dos downloads (qualquer usuário logado baixa qualquer
  arquivo); limites de tamanho de conteúdo; política de limpeza/retenção
  (hoje: cleanup só no startup, >24h, e `uploads/` sem volume persistente).
- **PERPETUA-PRODUCTION-DRIFT-01**: consolidar no repo os hotfixes feitos direto
  na VPS (`app/routers/leads.py` — `_build_lead_response(l)` sem `db`; e
  `INTERNAL_AI_AUTH_SECRET` no `docker-compose.yml`). **O deploy deste fix NÃO
  deve ocorrer antes dessa consolidação** — um deploy prematuro pode sobrescrever
  os hotfixes da VPS.
- Melhorias de renderização (tabelas reais, Markdown completo, fonte Unicode/emoji)
  ficam como FEATURE separada, se priorizadas.
