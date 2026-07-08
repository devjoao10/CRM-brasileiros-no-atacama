# Perpétua — Autenticação interna sem API Key por usuário

**Pacote:** PERPETUA-INTERNAL-AUTH-01 · **Tipo:** SECURITY · **Status:** corrigido localmente, testado localmente, **não deployado**.

## Resumo

Antes, as ferramentas internas da Perpétua (a IA do CRM) só funcionavam se o
usuário tivesse gerado manualmente uma **API Key** em *Configurações → API Key*.
Pior: o mecanismo estava **quebrado para todos** — o código enviava o *hash*
SHA‑256 armazenado como `X-API-Key`, e o validador re-hashava esse valor antes de
comparar, então nunca batia (a chave em texto puro só é exibida uma vez e nunca é
armazenada).

Agora **todo usuário autenticado** pode usar a Perpétua sem gerar nenhuma API Key.
As chamadas internas que a IA faz às rotas `/api/` são autenticadas por um
mecanismo **HMAC assinado no servidor**, agindo **em nome do usuário logado** e
preservando papel (role), permissões e atribuição/auditoria.

> A funcionalidade de **API Key continua existindo** e inalterada para
> integrações (n8n, automações) via header `X-API-Key`.

## Como funciona

1. No endpoint `POST /api/ai/chat`, o contexto do usuário logado é propagado para
   as ferramentas via `contextvars` (não mais um estado global mutável — resolve
   o risco ARCH‑04/RM‑01). Guarda `user_id`, `email`, `role`.
2. Quando a IA chama `call_internal_api`, o serviço **assina** a requisição com o
   segredo backend‑only `INTERNAL_AI_AUTH_SECRET` (HMAC‑SHA256), enviando:
   - `X-Internal-AI-User-Id`
   - `X-Internal-AI-Timestamp`
   - `X-Internal-AI-Signature`
   A assinatura cobre `user_id`, `timestamp`, método HTTP e o caminho (path).
3. A dependência `get_current_user` reconhece esses headers, valida a assinatura
   com `hmac.compare_digest`, checa a janela de tempo (`INTERNAL_AI_AUTH_MAX_SKEW_SECONDS`,
   padrão 300s), carrega o **usuário real** pelo id e o retorna — de modo que as
   checagens de papel (`require_admin`) e de propriedade continuam valendo.

O canal interno é **loopback** (`127.0.0.1:8000`); a assinatura autentica *quem*
chama e *qual* endpoint, e o timestamp limita replay.

### Falha segura (fail-safe)

- Se `INTERNAL_AI_AUTH_SECRET` **não** estiver configurado, as ferramentas
  internas ficam **desativadas**: a Perpétua ainda responde e faz `SELECT`s, mas
  `call_internal_api` recusa com uma mensagem clara (não pede API Key). O caminho
  de auth interno também não autentica ninguém (401).
- A Perpétua **nunca** vira um admin sem autenticação: ela sempre age como o
  usuário logado, com as permissões dele.

## Variáveis de ambiente (produção)

Adicionar em `/opt/crm/.env` **antes** do deploy:

```
# Segredo backend-only. NUNCA no frontend, NUNCA em respostas de API.
INTERNAL_AI_AUTH_SECRET=<gerar>
# Opcional (padrão 300)
INTERNAL_AI_AUTH_MAX_SKEW_SECONDS=300
```

Gerar o segredo com segurança:

```
openssl rand -base64 32
```

Verificar que está setado **sem imprimir o valor**:

```
grep -q '^INTERNAL_AI_AUTH_SECRET=.\+' /opt/crm/.env && echo "definido" || echo "AUSENTE"
```

## Banco read-only da IA (`crm_readonly`)

A ferramenta `run_select_query` usa a conexão read-only (`DATABASE_READONLY_URL`,
usuário `crm_readonly`). O tratamento de erro agora emite uma mensagem **segura**
quando a conexão/credencial falha (sem vazar senha; detalhe fica no log).

Validar a configuração **sem imprimir a senha**:

```
python scripts/check_readonly_db.py
```

Criar o usuário `crm_readonly` com segurança (exemplo — troque a senha e use a de
`CRM_READONLY_PASSWORD` do `.env`):

```sql
CREATE USER crm_readonly WITH PASSWORD '<CRM_READONLY_PASSWORD>';
GRANT CONNECT ON DATABASE crm_atacama TO crm_readonly;
GRANT USAGE ON SCHEMA public TO crm_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO crm_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO crm_readonly;
```

Em produção, `DATABASE_READONLY_URL` deve apontar para `crm_readonly` (somente
SELECT), **não** para o owner. Não há fallback para o banco de escrita.

## Nota sobre o hotfix de produção (SlowAPI)

O container de produção foi **corrigido manualmente** porque o SlowAPI exige um
parâmetro `request: Request` na rota `/api/ai/chat`. Este pacote torna a correção
**permanente no repositório**: a assinatura agora é
`ai_chat(request: Request, chat_request: ChatRequest, ...)` e usa
`request.app.openapi()`. Após o deploy deste PR, a dependência do patch manual no
container deixa de existir.

## Deploy (executado por humano — este pacote NÃO faz deploy)

1. Adicionar `INTERNAL_AI_AUTH_SECRET` em `/opt/crm/.env`.
2. Garantir `DATABASE_READONLY_URL` com senha válida do `crm_readonly`.
3. Rebuild/redeploy do `crm_app` normalmente (deploy manual/gated).
4. Verificar a Perpétua com um usuário **sem** api_key.
5. Remover qualquer dependência do hotfix aplicado direto no container.

## Rollback

Reverter o commit do pacote restaura o comportamento anterior (a Perpétua volta a
depender de API Key por usuário, que já estava quebrada). O segredo em `.env` pode
permanecer — sem código que o use, é inócuo. Nenhuma migração é necessária.
