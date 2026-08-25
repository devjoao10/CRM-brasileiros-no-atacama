# -*- coding: utf-8 -*-
"""
Aplica ao FINDINGS.csv os vereditos da Fase 2 e acrescenta os findings novos.

Regra desta fase, diferente da anterior: aqui NAO ha atribuicao mecanica. Cada
ID abaixo foi adjudicado LENDO o codigo de hoje, com a evidencia citada na
coluna `adjudicacao`. O que nao esta nas listas nao foi tocado.
"""
import csv
import io
import sys

REPO = sys.argv[1]
CSV = f"{REPO}/docs/audit/FINDINGS.csv"

# ── adjudicados lendo o codigo (agente revisor + verificacao minha) ────────
RESOLVED = {
    "F-252": "tags.py:91-98,129-136 try/except IntegrityError -> 409; teste test_crm_authz_hardening.py:203-206 executado",
    "F-253": "tasks.py:81-82 datetime.combine(..., tzinfo=utc) e :85 now(timezone.utc)",
    "F-254": "tasks.py:93 order_by(nullslast(data_vencimento), Task.id); teste test_crm_authz_hardening.py:241-251 executado",
    "F-255": "tasks.py:107-116 _assert_can_set_owner + _assert_lead_exists + model_dump(exclude={user_id})",
    "F-256": "tasks.py:142-145 checagem ANTES do setattr; teste test_crm_authz_hardening.py:145-155",
    "F-057": "schemas/task.py:24-29,38-42 comentario corrigido e portao real em tasks.py:107-108,142-143",
    "F-092": "webhook.py:181-236 try/except POR MENSAGEM com rollback; teste test_conversas_webhook_hardening.py:152-157 executado",
    "F-093": "mesma correcao; teste :178-202 'falha de INFRA -> 503' executado",
    "F-096": "media.py:88-101 classify_mime None -> octet-stream + attachment; teste test_conversas_media_storage.py:332,362-365",
    "F-100": "webhook.py:582 _remember_agent_cutoff ANTES de _send_auto_reply_if_needed; teste :232-253 executado",
    "F-107": "crm.py:308-317 INSERT em lead_history inclui `dados`",
    "F-108": "crm.py:71 igualdade EXATA de digitos e :74-79 recusa vinculo ambiguo",
    "F-325": "webhook.py:373-386 _customer_msg_at usa o timestamp da Meta; teste :377 executado (delta=0s)",
    "F-326": "webhook.py:389-401 _advance_customer_msg_at com max(); teste :396 executado",
    "F-327": "webhook.py:571-577 guarda de janela antes do auto-reply e do debounce",
    "F-330": "webhook.py:79 _STATUS_RANK + :611-623; teste :338-352 executado",
    "F-331": "mesma evidencia do F-330",
    "F-333": "webhook.py:871-875 order_by desc + limit + reverse",
    "F-531": "docker-compose.yml:162 META_APP_SECRET com `:?` — a pilha recusa subir sem o segredo",
    "F-535": "mesma evidencia do F-327",
    "F-536": "webhook.py:873 or_(direction != outbound, status != failed) + limit",
}

BLOQUEADOS = {
    "F-094": "indice unico em conversation.py:80 + teste, mas a m011 NAO foi executada — em producao o defeito segue vivo",
    "F-095": "mesma evidencia do F-094. A corrida de primeiro contato foi tratada em webhook.py nesta fase, ANTES da migration",
    "F-248": "Index uq_funnel_entries_lead_funnel em models/pipeline.py:50-52 + m011, nao executada",
    "F-328": "mesma evidencia do F-094; a metade em crm.auto_create_lead_in_crm segue sem unicidade em leads.whatsapp",
}

# findings de n8n reavaliados contra os exports ATUAIS de producao
OBSOLETOS = {
    "F-021": "workflow 'Gerente Autonomo de Tarefas IA' NAO esta entre os tres workflows de producao fornecidos em 2026-08-25",
    "F-022": "mesmo workflow ausente da producao",
    "F-023": "mesmo workflow ausente da producao — era ESTE o finding 'o LLM escolhe metodo E URL'",
}

ATUALIZADOS = {
    "F-019": ("BLOCKED_OPERATOR",
              "CONFIRMADO no export atual, com numero corrigido: sao 13 tools de CRM "
              "(nao 14) mais 1 morta (Tool Acionar Notificador). Webhook segue publico "
              "via Traefik em n8n.crmbrasileirosnoatacama.cloud"),
    "F-020": ("BLOCKED_OPERATOR",
              "CONFIRMADO: credencial 'CRM Brasileiros API' referenciada pelos tres "
              "workflows — a rotacao da chave precisa atualizar o n8n junto, senao os "
              "tres param (ver N8N_MANUAL_CHANGES.md D5)"),
    "F-024": ("OPEN",
              "PARCIAL no export atual: o system message cresceu de 20.470 para 28.697 "
              "chars mas continua sem instrucao de tratar texto do cliente como DADO. "
              "Ganhou defesa de SAIDA (node 'Validar saida da Bia'), que e outra coisa"),
    "F-025": ("FALSE_POSITIVE",
              "INVALIDADO pelo export atual: em 'Tool Enviar ao Gerenciador de Leads' o "
              "campo whatsapp e valueProvider=fieldValue vindo do webhook do Conversas, "
              "NAO escolhido pelo modelo. Residual menor: 'Tool Consultar Lead' tem "
              "{whatsapp} como placeholder, mas e LEITURA"),
    "F-026": ("BLOCKED_OPERATOR",
              "CONFIRMADO: /webhook/agent-bia segue publico e sem autenticacao; "
              "docker-compose.yml:117-121 publica o n8n no Traefik"),
    "F-029": ("RESOLVED",
              "tests/test_conversas_webhook_signature.py cobre a assinatura HMAC com o "
              "segredo LIGADO, incluindo corpo reembalado -> 403"),
}

# ── findings NOVOS da Fase 2 ──────────────────────────────────────────────
NOVOS = [
    ("N8N-F01", "CRITICAL", "CONFIRMED", "integracao/estado",
     "n8n/workflows/live_exports/20260825_fase2/wf01_agente_bia.json", "163", "163",
     "Tool Enviar ao Gerenciador de Leads",
     "O campo pronto_para_humano tem DOIS sinais de igual. No n8n o primeiro `=` marca a expressao e o resto e template: o `=` sobrando vira texto e o valor enviado e a string \"=true\"/\"=false\"",
     "valor literal: =={{ $fromAI('pronto_para_humano', ..., 'boolean', false) ? 'true' : 'false' }} — todos os outros campos do mesmo no usam UM `=`",
     "Rodar a triagem ate completar e ler o Output do node Webhook Gerenciador",
     "O system message do Gerenciador compara com \"true\"/\"false\" e o toolDescription de Tool Alterar Responsavel exige pronto_para_humano=true. Nenhum ramo casa com \"=true\": a transicao de estado mais importante do sistema (entrar na fila humana) fica decidida por um LLM sobre uma string que nao corresponde a regra nenhuma",
     "Apagar UM sinal de igual (N8N_MANUAL_CHANGES.md M1)",
     "nao automatizavel daqui — verificacao manual no n8n",
     "PROPOSED_FIX", "fase2-orq", "leitura do export + documentacao oficial do n8n via Context7"),

    ("N8N-F02", "CRITICAL", "CONFIRMED", "dependencia-morta",
     "n8n/workflows/live_exports/20260825_fase2/gerenciador_leads.json", "596", "596",
     "Tool Acionar Notificador",
     "Tool ativa apontando para POST http://n8n:5678/webhook/notificacao, workflow REMOVIDO da producao. Sem autenticacao e sem credencial",
     "no presente no export, ligado como ai_tool ao Agente Gerenciador de Leads",
     "Rodar uma conversa ate pronto_para_humano=true e observar a lista de tools chamadas",
     "404 devolve erro ao modelo. O agente tem retryOnFail=true e NENHUM onError, com saida unica para Responder ao Webhook: se falhar, a chamada da Bia fica sem resposta. E a ordem das tools e do modelo: chamar o notificador antes de Definir Tags/Alterar Responsavel pode deixar o lead criado sem tag e sem responsavel",
     "Remover o no e a conexao ai_tool. NAO substituir por outra notificacao (N8N_MANUAL_CHANGES.md M2)",
     "nao automatizavel daqui",
     "PROPOSED_FIX", "fase2-orq", "leitura do export + rastreio de conexoes"),

    ("N8N-F03", "HIGH", "CONFIRMED", "contrato/ux",
     "conversas/app/routers/webhook.py", "786", "800", "_fetch_agent_parts",
     "Silencio deliberado da Bia era tratado como degradacao: o node Ignorar mensagem responde 404 e todo nao-200 disparava o fallback",
     "Ignorar mensagem: respondWith=noData, responseCode=404; _fetch_agent_parts devolvia [] para todo status != 200",
     "Mandar uma mensagem contendo apenas um emoji",
     "Quem manda um emoji sozinho recebia 'Tive uma instabilidade para processar sua mensagem agora' — o oposto do que o portao foi feito para produzir — e cada reacao gravava linha de ERRO no log",
     "Metade feita: _fetch_agent_parts devolve (partes, silencio) e aceita 204/205/ignorar. A outra metade e o n8n responder 204 (M3)",
     "tests/test_conversas_agent_silence.py", "RESOLVED_PARCIAL", "fase2-orq",
     "leitura dos dois lados + teste executado"),

    ("N8N-F04", "HIGH", "CONFIRMED", "contrato",
     "app/schemas/lead.py", "132", "148", "LeadUpdate",
     "Tool Atualizar Lead tem jsonBody FIXO (manda as 12 chaves sempre, com \"\" no que nao foi coletado). LeadUpdate.nome tem min_length=1: toda atualizacao sem nome novo devolvia 422",
     "payload real rodado contra o schema: nome: String should have at least 1 character. min_length=1 identico em origin/main — defeito PRE-EXISTENTE",
     "PUT /api/leads/{id} com o corpo do template da tool",
     "O dado que a Bia acabara de coletar era descartado em silencio: o toolHttpRequest entrega o erro ao modelo, que segue conversando",
     "String vazia faz a CHAVE ser descartada (model_validator), entao exclude_unset a ignora. null explicito continua limpando. Guarda no router impede None em coluna NOT NULL",
     "tests/test_n8n_contract_lead_update.py", "RESOLVED", "fase2-orq",
     "payload do export rodado contra a ROTA, nao contra o schema"),

    ("N8N-F05", "HIGH", "CONFIRMED", "security/trust-boundary",
     "docker-compose.yml", "117", "121", "n8n traefik labels",
     "/webhook/gerenciador-leads e service-to-service (Bia -> Gerenciador) mas esta publico na internet, sem autenticacao, e o corpo e interpolado verbatim no prompt de um agente com 13 ferramentas de escrita no CRM",
     "traefik.http.routers.n8n.rule=Host(`n8n.crmbrasileirosnoatacama.cloud`); prompt: Processe o seguinte payload recebido da Bia: {{ JSON.stringify($json.body, null, 2) }}",
     "POST anonimo para o webhook com JSON arbitrario",
     "Injecao de prompt direta num agente que carrega a API key do CRM",
     "Header secreto no webhook, ou restricao por rede no Traefik, ou HMAC (N8N_MANUAL_CHANGES.md D1)",
     "nao automatizavel daqui", "BLOCKED_OPERATOR", "fase2-orq",
     "leitura do compose + do export"),

    ("N8N-F06", "HIGH", "CONFIRMED", "integridade/authz",
     "n8n/workflows/live_exports/20260825_fase2/formulario_site.json", "164", "169",
     "Atualizar lead existente",
     "O formulario publico busca lead por WhatsApp e, se achar, sobrescreve nome, email, destinos e datas — sem verificar que quem preencheu e dono do numero",
     "PUT http://crm:8000/api/leads/{id} com os campos do formulario; sem rate limit; CORS Access-Control-Allow-Origin: *",
     "Preencher o formulario publico com o WhatsApp de um cliente existente",
     "Anonimo sobrescreve o cadastro de um cliente real. O webhook e legitimamente publico, entao a solucao NAO e a mesma dos outros dois",
     "Decisao de produto: so criar; ou so preencher campo vazio; ou confirmar por WhatsApp (N8N_MANUAL_CHANGES.md D3)",
     "nao automatizavel daqui", "BLOCKED_OPERATOR", "fase2-orq", "leitura do export"),

    ("N8N-F08", "MEDIUM", "CONFIRMED", "encoding",
     "n8n/workflows/live_exports/20260825_fase2/gerenciador_leads.json", "484", "484",
     "Tool Adicionar Nota",
     "O texto da anotacao, escolhido pelo modelo, e concatenado CRU na query string",
     "PUT http://crm:8000/api/leads/{lead_id}/anotacoes?texto={texto} — o workflow do Formulario faz o certo no MESMO endpoint, com sendQuery",
     "Anotacao contendo & ou #",
     "A anotacao e truncada ou injeta parametro",
     "Mover texto para queryParameters (N8N_MANUAL_CHANGES.md M4)",
     "nao automatizavel daqui", "PROPOSED_FIX", "fase2-orq", "leitura do export"),

    ("N8N-F09", "MEDIUM", "EXTERNAL_STATE_UNVERIFIED", "config",
     "n8n/workflows/live_exports/20260825_fase2/wf01_agente_bia.json", "24", "24",
     "Gemini 2.5 Flash",
     "Os dois agentes usam modelName models/gemini-3.5-flash-lite num no rotulado 'Gemini 2.5 Flash'. Nao consigo verificar daqui se esse identificador existe",
     "modelName: models/gemini-3.5-flash-lite nos dois workflows",
     "Listar os modelos da API com a credencial do projeto",
     "Se nao existir, os dois agentes falham em toda execucao — e o fallback da Bia mascara isso como instabilidade",
     "Conferir com curl na API do Google e corrigir nos dois (N8N_MANUAL_CHANGES.md D4)",
     "nao automatizavel daqui", "BLOCKED_OPERATOR", "fase2-orq", "leitura do export"),

    ("N8N-F10", "MEDIUM", "CONFIRMED", "robustez",
     "n8n/workflows/live_exports/20260825_fase2/gerenciador_leads.json", "654", "654",
     "Agente Gerenciador de Leads",
     "O agente tem retryOnFail=true e NENHUM onError, com saida unica. O agente da Bia tem continueErrorOutput e um no de fallback",
     "retryOnFail: true, sem onError; connections com um unico grupo main",
     "Derrubar a credencial do modelo e disparar uma chamada",
     "Falha do agente = Responder ao Webhook nunca roda = a tool da Bia fica sem resposta ate o timeout de 240s",
     "Espelhar o ramo de erro da Bia (N8N_MANUAL_CHANGES.md M5)",
     "nao automatizavel daqui", "PROPOSED_FIX", "fase2-orq", "comparacao entre os dois exports"),

    ("N8N-F11", "LOW", "EXTERNAL_STATE_UNVERIFIED", "dependencia-nao-auditada",
     "n8n/workflows/live_exports/20260825_fase2/wf01_agente_bia.json", "182", "182",
     "consultar_contexto_bna",
     "Chama o subworkflow ZaCLNwNbQ84y4eAW (BIA — Consultar Knowledge Base), que nao foi fornecido",
     "workflowId.value: ZaCLNwNbQ84y4eAW",
     "-",
     "O system message manda tratar o retorno dele como FONTE DE VERDADE para decidir encaminhamento humano. Dependencia nao auditada de uma decisao de atendimento",
     "Exportar o subworkflow e auditar (N8N_MANUAL_CHANGES.md D6)",
     "nao automatizavel daqui", "BLOCKED_OPERATOR", "fase2-orq", "leitura do export"),

    ("N8N-F12", "MEDIUM", "CONFIRMED", "regressao-da-fase-1",
     "app/schemas/pipeline.py", "12", "40", "StageSchema.id",
     "O pattern ^[A-Za-z0-9_-]+$ introduzido na Fase 1 era risco de DISPONIBILIDADE: FunnelUpdate revalida a lista etapas INTEIRA, entao funil de producao com etapa contendo espaco ou acento daria 422 em QUALQUER edicao",
     "StageSchema(id='Sem Contato') era recusado — e o system message do Gerenciador chama a etapa exatamente assim",
     "PUT /api/pipeline/funnels/{id} num funil cuja etapa tenha espaco no id",
     "Edicao de funil quebrada em producao, por uma defesa que nao acrescentava seguranca sobre o escape ja existente no template",
     "Padrao passa a rejeitar o que quebra atributo HTML/literal JS e a aceitar espaco e acento",
     "tests/test_frontend_injection_contract.py secao 4", "RESOLVED", "fase2-orq",
     "verificacao empirica dos dois conjuntos"),

    ("PG-F01", "HIGH", "CONFIRMED", "dialeto/perda-de-dado",
     "conversas/app/routers/webhook.py", "64", "94", "_INFRA_ERRORS",
     "A lista era dialeto-dependente: coluna inexistente e valor fora do enum viram OperationalError no SQLite (na lista -> 503 -> a Meta reentrega) e ProgrammingError/DataError no PostgreSQL (fora -> 200 -> a Meta NUNCA reentrega)",
     "psycopg2.errors.lookup: 42703/42P01/42883 -> ProgrammingError; 22P02 -> DataError. Nenhum dos dois estava na tupla",
     "Provocar drift de schema em PostgreSQL e mandar um webhook",
     "Qualquer drift de schema em producao descartava mensagem de cliente em definitivo, e a suite SQLite demonstrava o comportamento OPOSTO",
     "ProgrammingError e DataError entraram na lista; IntegrityError segue fora de proposito",
     "tests/test_postgres_dialect_divergence.py secao 5", "RESOLVED", "fase2-agente-pg",
     "compilacao contra os dois dialetos + lookup por SQLSTATE"),

    ("PG-F02", "HIGH", "CONFIRMED", "dialeto/seguranca",
     "app/services/ai_tools.py", "155", "162", "run_select_query",
     "SELECT ... INTO passava por TODOS os guards: comeca com select, sem ponto e virgula, fora da denylist, nao cita users. No SQLite e erro de sintaxe; no PostgreSQL e DDL que CRIA TABELA",
     "a denylist bloqueava pragma e attach — palavras que so existem no SQLite",
     "Pedir a ferramenta de leitura da IA uma query com INTO",
     "A ferramenta 'somente leitura' que le texto de cliente vindo do WhatsApp podia criar tabela em producao. So o GRANT do crm_readonly a segurava",
     "into, copy, grant e revoke entraram na denylist. Revogar CREATE no schema continua sendo acao de operador",
     "tests/test_postgres_dialect_divergence.py secao 8", "RESOLVED", "fase2-agente-pg",
     "compilacao + execucao contra SQLite"),

    ("PG-F03", "MEDIUM", "CONFIRMED", "dialeto/ordenacao",
     "conversas/app/routers/conversations.py", "581", "582", "fila legada",
     "order_by sobre coluna anulavel sem clausula NULLS: SQL identico nos dois dialetos, defaults OPOSTOS (SQLite NULL primeiro, PostgreSQL por ultimo)",
     "Conversation.last_customer_msg_at.asc() sem nullsfirst/nullslast; _inbox_order ja usava nullslast",
     "Listar a fila legada com uma conversa sem inbound do cliente",
     "Conversa sem inbound abria a fila em teste e fechava a fila em producao",
     ".nullslast() explicito",
     "tests/test_postgres_dialect_divergence.py secao 4", "RESOLVED", "fase2-agente-pg",
     "compilacao contra os dois dialetos"),

    ("BKP-F01", "CRITICAL", "CONFIRMED", "regressao-da-fase-1",
     "scripts/backup_postgres.sh", "97", "135", "verificacoes 2 e 3",
     "pipefail + SIGPIPE invertiam as verificacoes: grep -q sai no primeiro casamento e mata o gzip a montante; com pipefail o status do pipeline vira 141 e o `if !` inverte a guarda. Vale para todo dump maior que o buffer do pipe",
     "reproduzido: gzip -dc ok.gz | grep -qE 'COPY public.users' -> status 141 com pipefail LIGADO, 0 com DESLIGADO",
     "Rodar o script com um dump valido de mais de 64KB",
     "O script da Fase 1 abortaria TODO backup real. E o mesmo mecanismo na guarda de CR faria um dump corrompido passar batido",
     "set +o pipefail em volta das verificacoes, reativado depois",
     "tests/test_backup_restore_e2e.py", "RESOLVED", "fase2-agente-backup",
     "execucao do script com docker falso; reproducao independente do SIGPIPE"),

    ("BKP-F02", "HIGH", "CONFIRMED", "regressao-da-fase-1",
     "scripts/backup_postgres.sh", "127", "129", "guarda anti-CR",
     "A guarda anti-CR usava grep, e o grep da familia Cygwin/MSYS descarta o CR final de cada linha antes de casar — justamente o CR que o pseudo-TTY produz",
     "sobre dump com 7746 CRs em 1MB: guarda original status=1 (nao detecta), guarda nova conta 7746",
     "Rodar o script com um dump corrompido com CRLF fora do Linux",
     "A protecao que a Fase 1 dizia ter instalado nao funcionava fora do Linux",
     "tr -dc '\\r' | wc -c, que conta bytes sem nocao de linha",
     "tests/test_backup_restore_e2e.py cenario 3", "RESOLVED", "fase2-agente-backup",
     "execucao com dump corrompido"),

    ("BKP-F03", "HIGH", "CONFIRMED", "qualidade-de-teste",
     "tests/test_filter_normalization_and_backup.py", "117", "147", "secao 3",
     "O teste da Fase 1 verificava o TEXTO do script (\"gzip -t\" in sh) e nunca o EXECUTAVA. Um teste de grep nao pode achar um bug de propagacao de status de pipeline",
     "check('gzip -t' in sh, ...), check('trap cleanup EXIT' in sh, ...)",
     "-",
     "Foi por isso que BKP-F01 e BKP-F02 passaram batidos. E exatamente a classe de defeito que esta auditoria mais encontrou, no meu proprio trabalho",
     "tests/test_backup_restore_e2e.py executa o script de ponta a ponta em 7 cenarios",
     "tests/test_backup_restore_e2e.py", "RESOLVED", "fase2-agente-backup",
     "comparacao entre o que o teste antigo afirmava e o que a execucao revelou"),

    ("CONV-F01", "MEDIUM", "CONFIRMED", "concorrencia/pre-migration",
     "conversas/app/routers/webhook.py", "514", "545", "_process_incoming_message",
     "busca-e-cria-se-nao-achar sem lock na conversa. Hoje a corrida cria conversa duplicada; com uq_conversations_whatsapp (m011) ativo o perdedor levantaria IntegrityError, que NAO esta em _INFRA_ERRORS de proposito e viraria 200",
     "db.add(conversation); db.flush() sem tratamento de IntegrityError",
     "Duas mensagens do mesmo numero chegando juntas",
     "A migration trocaria um defeito VISIVEL (conversa duplicada) por um INVISIVEL (mensagem de cliente perdida, com 200 para a Meta)",
     "Quem perde a corrida releva, re-busca e segue com a linha do vencedor. Aplicado ANTES de a m011 rodar",
     "coberto indiretamente por test_conversas_webhook_hardening.py; sem teste de corrida real",
     "RESOLVED", "fase2-agente-revisor", "leitura do codigo + do plano da m011"),
]

# ── aplica ──────────────────────────────────────────────────────────────────
linhas = list(csv.DictReader(io.open(CSV, encoding="utf-8")))
campos = list(linhas[0].keys())

por_id = {r["id"]: r for r in linhas}
cont = {"resolved": 0, "bloqueado": 0, "obsoleto": 0, "atualizado": 0, "novo": 0}

for fid, razao in RESOLVED.items():
    if fid in por_id:
        por_id[fid]["status"] = "RESOLVED"
        por_id[fid]["adjudicacao"] = "FASE2 (revisao manual): " + razao
        cont["resolved"] += 1

for fid, razao in BLOQUEADOS.items():
    if fid in por_id:
        por_id[fid]["status"] = "BLOCKED_OPERATOR"
        por_id[fid]["adjudicacao"] = "FASE2 (revisao manual): " + razao
        cont["bloqueado"] += 1

for fid, razao in OBSOLETOS.items():
    if fid in por_id:
        por_id[fid]["status"] = "OBSOLETE"
        por_id[fid]["adjudicacao"] = "FASE2 (evidencia externa nova): " + razao
        cont["obsoleto"] += 1

for fid, (st, razao) in ATUALIZADOS.items():
    if fid in por_id:
        por_id[fid]["status"] = st
        por_id[fid]["adjudicacao"] = "FASE2 (reavaliado contra o export atual): " + razao
        cont["atualizado"] += 1

for tupla in NOVOS:
    r = dict(zip(
        ["id", "severity", "confidence", "category", "file", "line_start",
         "line_end", "symbol", "description", "evidence", "reproduction",
         "impact", "recommended_fix", "regression_test", "status", "reviewer",
         "tools_used"], tupla))
    linha = {c: "" for c in campos}
    for k, v in r.items():
        if k in linha:
            linha[k] = v
    linha["root_cause_candidate"] = ""
    linha["adjudicacao"] = "FASE2: finding novo, criado a partir da evidencia externa"
    linhas.append(linha)
    cont["novo"] += 1

with io.open(CSV, "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=campos)
    w.writeheader()
    w.writerows(linhas)

import collections
c = collections.Counter(r["status"] for r in linhas)
sev = collections.Counter(
    r["severity"] for r in linhas
    if r["status"] in ("OPEN", "ADDRESSED_UNVERIFIED"))
print("aplicado:", cont)
print("total de findings:", len(linhas))
print("por status:", dict(c.most_common()))
print("NAO resolvidos nem bloqueados, por severidade:", dict(sev))
