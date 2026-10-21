# Registro — CONV-BIA-FILA-01 + CONV-EXPEDIENTE-01 (Bia ativa na fila + frases de fora do expediente)

| Campo | Valor |
|---|---|
| Branch | `bia-atendimento-config-16d588` |
| Base | `d84c4ce` |
| Plano | `docs/superpowers/plans/2026-10-06-bia-ativa-fila-e-fora-de-expediente.md` (decisões D1–D11) |
| Modo | Implementação local. **Sem push/PR/deploy/VPS/banco de produção. Sem migration. Sem mudança no n8n.** |

## Pedido do dono (2026-10-06)
1. A Bia era desligada assim que a conversa saía da triagem e entrava na fila de espera. Ela deve
   **continuar ativa** na fila; quem a desliga é a vendedora, manualmente.
2. As frases automáticas de **fora do expediente** devem funcionar: depois que a Julia desliga a Bia,
   clientes cobram atendimento de madrugada e isso deve ser tratado com mensagem automática.

## Diagnóstico (auditoria desta rodada)
- Cadeia real do handoff em produção: Bia (n8n) → Gerenciador → workflow n8n "Handoff Humano → Julia":
  `PUT /api/leads/{id}/responsavel` (CRM, que pela ponte chama `POST /api/conversations/by-lead/{id}/handoff`)
  → `POST /by-lead/{id}/handoff` → `POST /{id}/assign {"user_id": 5}`. Handoff **e** assign passavam
  por `aplicar_estado_humano`, que gravava `is_bot_active = False`.
- As abas do inbox dependiam só do flag (`bia` = bot ligado; `fila`/`todos` = bot desligado): manter a
  Bia ligada sem mudar os predicados faria a conversa sumir da fila.
- O prompt da Bia no n8n já previa continuar respondendo depois da fila; o backend destoava.
- Fora do expediente: o envio existia (`webhook.py`), mas (a) produção não roda seed e a tela de
  Configurações só **editava** linhas existentes — com as tabelas vazias não havia como cadastrar a
  frase nem o horário; (b) `@NOMEATENDENTE`/`@EMAILATENDENTE` nunca resolviam no webhook e a frase era
  pulada em silêncio; (c) sem anti-spam; (d) o toggle da tela dizia "ativada" mesmo com falha.

## Entregue
- **Bia ativa na fila** — `aplicar_estado_humano(..., desligar_bia=True)`; handoff (conversa e lead),
  assign, release e `PUT` que só troca atendente **preservam** o flag e nunca religam a Bia.
  Continuam desligando: botão "Desativar Bot", "Assumir" (claim), primeira resposta humana e initiate.
  "Desativar Bot" funciona mesmo se o dono atual tiver sido desativado.
- **Abas** — `queued_at` também classifica o lado humano: `bia` = aberta ∧ bot ligado ∧ fora da fila;
  `fila`/`todos` aceitam bot desligado **ou** na fila; `meus`/`encerradas` inalteradas. Selo
  "aguardando humano" do inbox idem. `VERSION` 1.0.1 (cache dos estáticos).
- **Bia não atropela a vendedora** — a resposta do agente que volta depois de a Bia ser desligada ou de
  a conversa ser encerrada (espera de até 240s) é descartada, inclusive o fallback; a falha degradada
  preserva o dono.
- **Sinal para o n8n** — payload ganha `na_fila_humana` (ver pré-requisito abaixo).
- **Fora do expediente** — aviso só quando a mensagem **não** vai para a Bia; no máximo 1 por conversa a
  cada 12h (mesmo texto, status ≠ `failed`; mensagem da vendedora não conta); guarda contra mensagens
  simultâneas (várias fotos de uma vez geram 1 aviso); variáveis de atendente resolvem para o dono da
  conversa (só nome/e-mail).
- **Configurações** — catálogo `AUTO_REPLY_PADROES` (fonte única do seed e da tela); a tela lista todas
  as frases e os 7 dias mesmo com o banco vazio e cria o que faltar ao salvar; **salvar exige admin**
  (leitura segue aberta a qualquer usuário logado) e o log registra quem alterou; valida `HH:MM`
  (dígitos ASCII), no máximo 7 dias sem repetição, fechamento > abertura, frase ≤ 4096 caracteres e
  sem NUL; recusa ativar frase vazia; aviso explícito de horário ainda não salvo; erro cita o dia da
  semana; toggle salva o texto visível.
- **CRM** — alerta da troca de responsável (`templates/pipeline.html`) não afirma mais que a conversa
  "saiu da Bia".
- **Guarda da V1** — baseline de hash de `atendimento.py`, `conversations.py` e `webhook.py`
  recalculado à mão em `tests/test_v2_v1_intacta.py` (pedido explícito do dono).
- **`migrations/README.md`** — aviso para não re-executar a m008.

## Provas
- Testes novos: `test_conversas_bia_ativa_fila.py`, `test_conversas_fora_expediente.py`,
  `test_conversas_bia_agente_fila.py`, `test_conversas_settings_expediente.py`; asserts do comportamento
  antigo atualizados em `test_conversas_operational_state.py`.
- Revisão por tarefa (spec + qualidade) e revisão final do branch inteiro com os quatro revisores do
  CLAUDE.md (geral, segurança, FastAPI, Python); achados corrigidos numa rodada final.
- Smoke no app real (local, SQLite descartável, tabelas de configuração vazias como em produção):
  abas corretas; "Desativar Bot" desliga e preserva dona/posição; tela de Configurações cria a frase e
  os horários; webhook fora do horário envia 1 aviso e não repete na mensagem seguinte.
- Suítes completas: ver o resultado final registrado no fechamento desta entrega.

## PRÉ-REQUISITO de deploy — n8n (WF-01 "Agente Bia")
Com a Bia ligada na fila, se o modelo decidir de novo `pronto_para_humano = true`, o workflow "Handoff
Humano → Julia" roda outra vez: o responsável no CRM volta para o usuário 5, o atendente volta para a
Julia (desfazendo uma reatribuição manual) e o Gerenciador pode duplicar nota/tarefa. A trava
determinística fica no n8n:

1. No WF-01, nó **`Tool Enviar ao Gerenciador de Leads`** (toolHttpRequest), parâmetro de corpo
   **`pronto_para_humano`** (hoje:
   `={{ $fromAI(  'pronto_para_humano',  'Use true somente quando o atendimento deve ser encaminhado para humano. Use false para apenas atualizar ou registrar dados.',  'boolean',  false) ? 'true' : 'false' }}`),
   trocar por:
   `={{ ( $fromAI(  'pronto_para_humano',  'Use true somente quando o atendimento deve ser encaminhado para humano. Use false para apenas atualizar ou registrar dados.',  'boolean',  false) && $('Webhook Mensagem').first().json.body.na_fila_humana !== true ) ? 'true' : 'false' }}`
   (nomes conferidos no export de 2026-08-26 — confirme no editor ao vivo que o gatilho continua se
   chamando `Webhook Mensagem`).
2. Conferir no editor ao vivo que o workflow **"Handoff Humano → Julia"** só chama
   `PUT /api/leads/{id}/responsavel`, `POST /api/conversations/by-lead/{id}/handoff` e
   `POST /api/conversations/{id}/assign` — e **não** `PUT /api/conversations/{id}` com
   `is_bot_active: false` nem `/claim` (se chamar, a Bia continuaria sendo desligada no handoff).
3. Opcional: uma linha no prompt da Bia para, com `na_fila_humana`, dizer que a equipe já foi avisada em
   vez de anunciar uma nova entrada na fila.

## Depois do deploy (ação de um admin)
1. Conversas → Configurações → **Horário Comercial**: ajustar os horários reais e **Salvar Horários**
   (até salvar, todos os dias contam como abertos o dia todo e o aviso não sai).
2. Conversas → Configurações → **Frases Automáticas** → ativar **só** "Fora do expediente" (revisar o
   texto). "Frase de apresentação" e "Aguardando atendimento" saem mesmo com a Bia respondendo;
   "Início de atendimento", "Intervalo" e "Status em pausa" não são usados pelo sistema.
3. Antes do deploy, conferir se alguma frase já gravada usa `@NOMEATENDENTE`/`@EMAILATENDENTE`: elas
   passam a ser enviadas (antes eram puladas).
4. Avisar as vendedoras: na fila a Bia continua respondendo até alguém clicar em **"Assumir"** ou
   **"Desativar Bot"**, ou mandar a primeira resposta. Cada resposta da Bia zera o contador de não
   lidas; o selo **"aguardando humano"** é o sinal de quem espera.
5. Nenhuma migration a aplicar.

## Riscos aceitos e observações
- **Não re-executar a m008** (nota em `migrations/README.md`).
- **V2 diverge**: `WAITING_HUMAN`/`HUMAN_ACTIVE` exigem bot desligado (`app/v2/estados.py`, CHECK m015).
  Reconciliar a regra nova antes de promover a V2.
- A guarda contra avisos simultâneos e o debounce da Bia são em memória: valem com **um** processo
  uvicorn (configuração atual). Escalar para vários workers exige trava no banco.
- O re-check que descarta a resposta da Bia acontece antes do envio; numa resposta em várias partes,
  as partes seguintes não são re-checadas (janela de poucos segundos).
- O cooldown de 12h compara o texto enviado: editar a frase ou usar variáveis de data
  (ex.: data de hoje) libera um novo aviso.
- Fuso do horário comercial continua UTC-3 fixo (horário de Brasília).
- Configurações: dois saves simultâneos da mesma frase inexistente podem dar 500 no segundo (tela de
  baixo tráfego; repetir resolve).
