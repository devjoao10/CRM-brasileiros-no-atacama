# BNA V3 — Roadmap

## Motivação

A BIA V2 entrega atendimento via WhatsApp com IA usando dois apps FastAPI (CRM e Conversas) sobre um único PostgreSQL compartilhado, orquestrados pelo n8n como motor de decisão de roteamento de mensagens, com um mecanismo de CAS (compare-and-swap) entre apps para manter a titularidade do lead consistente a cada handoff bot → humano.

Essa arquitetura — dois apps mais n8n decidindo negócio — foi a origem dos problemas mais difíceis do projeto: races de concorrência resolvidas via CAS com SELECT FOR UPDATE, maquinaria de idempotência/replay, workers de retry/reconciliação, e uma camada de compatibilidade V1/V2 para impedir que código legado corrompesse silenciosamente o novo estado. Tudo isso funciona e vai para produção, mas é mais pesado do que a equipe quer carregar como base permanente.

A V2 não é a arquitetura final. Assim que estiver operando, começa um novo projeto, "BNA V3", desenhado desde o início para nunca mais precisar dessa classe de correção. O objetivo é remover estruturalmente essa classe de problema — não corrigi-la mais uma vez.

## Direção arquitetural

- **Sem n8n no core**: n8n orquestrando decisões de negócio é exatamente o que a V3 remove.
- **Monólito modular**: acaba com a divisão em apps separados que forçou o CAS entre CRM e Conversas.
- **FastAPI**: mantém a stack já validada pela equipe, sem custo de reescrita nem nova camada de compatibilidade.
- **Um único banco PostgreSQL autoritativo** (não dois bancos sincronizados): elimina toda a classe de problema de CAS/reconciliação entre apps que a V2 teve que resolver.
- **Workers Python próprios** (não um motor de workflow externo): assumem retry/reconciliação sem depender de um motor externo decidindo negócio.
- **Padrão Outbox** para entrega confiável de eventos, sem o aparato de replay que a V2 precisou construir por cima do n8n.
- **Inbox/idempotência** para consumo confiável de eventos, nativo desde o início — não retrofitado depois do problema aparecer.
- **Integração com a Meta (WhatsApp) direto no backend**, sem bridge/orquestrador externo na frente, removendo um salto que hoje exige reconciliação.
- **Conversa, lead e atendimento no mesmo domínio**: corrige direto o problema de ownership entre apps da V2 — com um domínio só, não existe uma segunda cópia de "quem é dono deste lead" para dessincronizar.
- **IA "interpretadora" sem tools**: um passo de interpretação simples, não um agente com tool-calling decidindo negócio.
- **Motor de regras determinístico** (não a IA) decide as transições de negócio/estado: a decisão sai do n8n e não vai parar na IA.
- **IA "comunicadora"**: passo separado que transforma a decisão do motor de regras em mensagem ao cliente, sem misturar decisão de estado com geração de texto.
- **Guard de saída** sobre o que a IA produz antes de chegar ao cliente: mesma preocupação que levou a V2 a precisar de uma camada de compatibilidade contra corrupção silenciosa de estado.
- **Frontend simples** (detalhado na seção própria).

## Stack preferida (ponto de partida)

Direção de partida, não implementação: FastAPI, PostgreSQL, SQLAlchemy, uma abordagem de frontend simples (Jinja + HTMX, ou equivalente — sem framework de SPA pesado), um processo de worker Python simples, uma tabela de outbox no PostgreSQL, e `SELECT ... FOR UPDATE SKIP LOCKED` para coordenação de claim entre workers.

Restrição inicial deliberada (não é veto permanente): evitar, ao menos no início, Redis, Kafka, RabbitMQ, microsserviços, n8n como motor, e sincronização entre dois bancos operacionais separados.

## Frontend — princípios

- Interface muito mais simples que um CRM enterprise típico: o operador entende a tela em segundos.
- Tela principal em três colunas: (1) lista de conversas, (2) chat, (3) dados essenciais do lead.
- Ações primárias: "Assumir" (tomar da bot), "Devolver para Bia" (devolver para a IA), "Encerrar", "Mover etapa" (mover estágio do pipeline).
- Evitar explicitamente: excesso de cards, dezenas de filtros, menus profundos/aninhados, dashboards desnecessários, abstrações de CRM enterprise que o operador não precisa.

## O que este documento não é

Um esboço de direção — não uma especificação de arquitetura completa, nem um plano de implementação com tarefas ou estimativas. Deliberadamente, não aprofunda além dos pontos listados acima.

## Nota final

Isto é apenas um esboço de roadmap. Nenhuma implementação começa agora como resultado deste documento. Nenhuma mudança de código da V2 decorre daqui. Isto não é uma decisão de abandonar a V2. A V2 vai para produção primeiro.
