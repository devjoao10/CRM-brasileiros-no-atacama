/**
 * Conversas.js — Main application logic
 * Handles conversation list, chat, lead info panel, and CRM integration.
 */

(function () {
    'use strict';

    // ─── State ──────────────────────────────────
    let conversations = [];
    let activeConversation = null;
    // PACOTE-B: categoria do inbox (server-side). Substitui activeFilter, que
    // filtrava em JS sobre a pagina carregada.
    const INBOX_KEYS = ['meus', 'fila', 'bia', 'todos', 'encerradas'];
    const INBOX_LABELS = {
        meus: 'Meus atendimentos',
        fila: 'Fila de espera',
        bia: 'Atendimentos BIA',
        todos: 'Todos',
        encerradas: 'Encerradas',
    };
    const INBOX_EMPTY = {
        meus: 'Nenhum atendimento seu no momento.',
        fila: 'Nenhum cliente aguardando atendimento.',
        bia: 'Nenhum atendimento com a BIA no momento.',
        todos: 'Nenhum atendimento humano aberto.',
        encerradas: 'Nenhuma conversa encerrada.',
    };
    const INBOX_STORAGE_KEY = 'conversas_inbox';
    const PAGE_SIZE = 50;
    // Teto do parametro `limit` no backend (Query(..., le=200)). O polling
    // nunca pede mais que isso numa tacada.
    const MAX_PAGE_LIMIT = 200;
    let activeInbox = 'meus';
    // Tamanho da JANELA atualmente carregada. NAO e um segundo sistema de
    // paginacao: e so quantas linhas o polling deve reconsultar a partir do
    // offset 0 para nao jogar fora o que o usuario carregou com "Carregar mais".
    let loadedWindowSize = PAGE_SIZE;
    let inboxCounts = {};
    let listTotal = 0;
    let listError = false;
    // Guarda de corrida: so a resposta do pedido MAIS RECENTE pode renderizar.
    // Sem isto, clicar Fila e logo BIA deixaria a resposta lenta da Fila
    // sobrescrever a categoria ja selecionada.
    let listRequestSeq = 0;
    let activeResponsavelFilter = '';
    let activeTagFilter = '';   // CONV-05
    let allTags = [];           // CONV-05
    let searchTerm = '';
    let pollInterval = null;
    let usersCache = [];

    // CRM base URL
    const CRM_BASE_URL = window.location.hostname === 'localhost'
        ? 'http://localhost:8000'
        : 'https://crm.crmbrasileirosnoatacama.cloud';

    // CONV-HOTFIX-POSTDEPLOY-01: o codigo antigo aceitava qualquer status no
    // PUT, entao podem existir linhas LEGADAS com status='aguardando'
    // persistido — contam como abertas (mesma tolerancia do backend).
    const isOpenStatus = (s) => s === 'aberta' || s === 'aguardando';

    // ─── CONV-NOTIFICATIONS-01: notificacoes leves de novas mensagens ───
    // Camada 100% frontend sobre o polling existente. Sinais:
    //  - lista: delta de unread_count por conversa (verdade do servidor para
    //    inbound; baseline na PRIMEIRA carga — nunca notifica historico);
    //  - conversa ABERTA: Set de ids de mensagens inbound ja vistas (o poll
    //    de detail zera unread no backend, entao o delta da lista nao cobre
    //    a conversa aberta — e por isso a lista PULA a conversa aberta,
    //    evitando notificacao duplicada da mesma mensagem).
    // Texto de notificacao do navegador e SEMPRE generico (nunca nome,
    // telefone ou conteudo do cliente). Sem service worker, sem Web Push.
    // Qualquer falha aqui e engolida — notificacao nunca quebra o chat.
    const notificationState = {
        baselined: false,          // primeira carga da lista ja virou baseline?
        unreadByConv: new Map(),   // conv.id -> unread_count da ultima carga
        seenInboundIds: new Set(), // ids inbound ja vistos (conversa aberta)
        chatBaselined: new Set(),  // conv.ids com historico do chat baselinado
        pending: 0,                // novas mensagens desde o ultimo "ack"
        originalTitle: document.title,
        soundReady: false,         // so toca depois da 1a interacao (autoplay)
        soundMuted: false,
        audioCtx: null,
    };

    function updateConvNotificationUi() {
        const badge = document.getElementById('convNotificationCount');
        if (badge) {
            if (notificationState.pending > 0) {
                badge.textContent = String(notificationState.pending);
                badge.style.display = 'inline-flex';
            } else {
                badge.style.display = 'none';
            }
        }
        document.title = notificationState.pending > 0
            ? `(${notificationState.pending}) ${notificationState.originalTitle}`
            : notificationState.originalTitle;
    }

    function ackConvNotifications() {
        notificationState.pending = 0;
        updateConvNotificationUi();
    }

    function playNotificationBeep() {
        if (!notificationState.soundReady || notificationState.soundMuted) return;
        try {
            if (!notificationState.audioCtx) {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) return;
                notificationState.audioCtx = new Ctx();
            }
            const ctx = notificationState.audioCtx;
            if (ctx.state === 'suspended') ctx.resume();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.frequency.value = 880;
            gain.gain.value = 0.04;
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.12);
        } catch (_) { /* audio bloqueado/sem suporte: silencio, nunca quebra */ }
    }

    function showBrowserNotification() {
        try {
            if (!('Notification' in window)) return;
            if (Notification.permission !== 'granted') return;
            if (!document.hidden) return; // OS notification so com a aba oculta
            // Texto GENERICO fixo — nunca nome, telefone ou mensagem do cliente.
            new Notification('Nova mensagem no Conversas', {
                body: 'Há uma nova mensagem aguardando atendimento.',
                tag: 'conv-nova-mensagem', // colapsa notificacoes repetidas
            });
        } catch (_) { /* negado/sem suporte: fallback silencioso p/ titulo */ }
    }

    function notifyNewInbound(novas) {
        try {
            notificationState.pending += novas;
            updateConvNotificationUi();
            playNotificationBeep();
            showBrowserNotification();
        } catch (err) {
            console.warn('Notificacao ignorada (nao afeta o chat):', err);
        }
    }

    // Delta de nao-lidas do UNIVERSO ABERTO, vindo do /counts.
    //
    // PACOTE-B: antes esta deteccao lia o array da listagem, que era a lista
    // INTEIRA sem filtro. Com o inbox filtrando no servidor, ler a lista
    // deixaria o operador cego para mensagens fora da aba selecionada — por
    // isso a fonte agora e o mapa `unread` do /counts, independente da
    // categoria. A semantica (baseline na 1a carga, delta positivo por
    // conversa, conversa ABERTA tratada pelo processChatNotifications)
    // permanece exatamente a mesma.
    function processUnreadNotifications(unreadMap) {
        try {
            const st = notificationState;
            const cur = new Map();
            Object.keys(unreadMap).forEach(k => cur.set(Number(k), Number(unreadMap[k]) || 0));
            if (!st.baselined) {
                st.unreadByConv = cur;
                st.baselined = true; // 1a carga = baseline: NUNCA notifica
                return;
            }
            let novas = 0;
            cur.forEach((n, id) => {
                // conversa aberta: deteccao pertence ao processChatNotifications
                if (activeConversation && id === activeConversation.id) return;
                const prev = st.unreadByConv.get(id);
                const delta = (prev === undefined) ? n : n - prev;
                if (delta > 0) novas += delta;
            });
            st.unreadByConv = cur;
            if (novas > 0) notifyNewInbound(novas);
        } catch (err) {
            console.warn('Notificacao (unread) ignorada:', err);
        }
    }

    // Mensagens da conversa ABERTA (loadChat abre = baseline silencioso;
    // poll de detail = novas inbound notificam apenas com a aba oculta).
    function processChatNotifications(data, fromPoll) {
        try {
            const st = notificationState;
            let novas = 0;
            (data.messages || []).forEach(m => {
                if (m.direction !== 'inbound' || !m.id) return;
                if (st.seenInboundIds.has(m.id)) return;
                st.seenInboundIds.add(m.id);
                if (fromPoll && st.chatBaselined.has(data.id)) novas++;
            });
            st.chatBaselined.add(data.id);
            if (novas > 0 && document.hidden) notifyNewInbound(novas);
        } catch (err) {
            console.warn('Notificacao (chat) ignorada:', err);
        }
    }

    function setupConvNotificationControls() {
        try {
            const btn = document.getElementById('convNotificationEnable');
            if (btn) {
                // botao so aparece quando ha suporte E a permissao ainda nao
                // foi decidida; NUNCA pedimos permissao no load da pagina.
                if (('Notification' in window) && Notification.permission === 'default') {
                    btn.style.display = 'inline-flex';
                }
                btn.addEventListener('click', async () => {
                    try {
                        if (!('Notification' in window)) { btn.style.display = 'none'; return; }
                        const perm = await Notification.requestPermission();
                        if (perm !== 'default') btn.style.display = 'none';
                        showToast(perm === 'granted'
                            ? 'Notificações do navegador ativadas'
                            : 'Notificações não autorizadas — o contador na aba continua funcionando');
                    } catch (_) { /* sem suporte: segue com titulo/badge */ }
                });
            }
            const mute = document.getElementById('convNotificationMute');
            if (mute) {
                mute.addEventListener('click', () => {
                    notificationState.soundMuted = !notificationState.soundMuted;
                    mute.classList.toggle('muted', notificationState.soundMuted);
                    mute.title = notificationState.soundMuted
                        ? 'Som de notificação desativado'
                        : 'Som de notificação ativado';
                });
            }
            // Autoplay policy: som liberado apenas apos a 1a interacao real.
            document.addEventListener('pointerdown', () => {
                notificationState.soundReady = true;
            }, { once: true });
            // Focar/voltar para a aba "da o visto" nas novas mensagens.
            window.addEventListener('focus', ackConvNotifications);
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) ackConvNotifications();
            });
        } catch (err) {
            console.warn('Controles de notificacao indisponiveis:', err);
        }
    }
    // ─── fim CONV-NOTIFICATIONS-01 ───

    // ─── CONV-MOBILE-PWA-01: fluxo mobile (lista -> chat -> voltar) ───
    // Completa o padrao off-canvas PRE-EXISTENTE (.conv-sidebar vira drawer
    // no breakpoint 640px e #mobileBack ja tinha handler que o reabre):
    // em telas pequenas a LISTA abre primeiro em tela cheia; tocar numa
    // conversa fecha o drawer (chat em tela cheia); voltar reabre a lista.
    // Sem dependencias, sem keydown global; falha aqui nunca quebra o chat.
    const mobileLayoutQuery = window.matchMedia('(max-width: 640px)');

    function isMobileLayout() {
        return mobileLayoutQuery.matches;
    }

    function setupMobileLayout() {
        try {
            if (isMobileLayout()) {
                // mobile comeca pela lista (drawer aberto em tela cheia)
                document.getElementById('convSidebar').classList.add('open');
            }
            // redimensionar desktop -> mobile sem conversa aberta: mostra a
            // lista (senao o usuario ficaria preso no estado vazio do chat)
            if (mobileLayoutQuery.addEventListener) {
                mobileLayoutQuery.addEventListener('change', (e) => {
                    try {
                        if (e.matches && !activeConversation) {
                            document.getElementById('convSidebar').classList.add('open');
                        }
                    } catch (_) { /* no-op */ }
                });
            }
        } catch (_) { /* layout mobile nunca derruba o app */ }
    }

    function closeMobileDrawerForChat() {
        try {
            if (isMobileLayout()) {
                document.getElementById('convSidebar').classList.remove('open');
            }
        } catch (_) { /* no-op */ }
    }
    // ─── fim CONV-MOBILE-PWA-01 ───

    // ─── CONV-MOBILE-RESPONSIVE-02: info do contato como subview mobile ───
    // Em <=1200px o CONV-MOBILE-PWA-01 esconde o #leadPanel com !important
    // (vencia o display inline do loadChat) — o que deixou as informacoes do
    // contato INACESSIVEIS no mobile/tablet. Aqui o mesmo painel (mesmo
    // markup, sem duplicar) reabre em tela cheia via body.conv-mobile-info-open;
    // #btnToggleInfo abre, #btnCloseInfoMobile / voltar fecham. Estado so e
    // usado sob matchMedia; desktop >1200px mantem o toggle inline original.
    const infoLayoutQuery = window.matchMedia('(max-width: 1200px)');

    function openMobileInfoPanel() {
        try { document.body.classList.add('conv-mobile-info-open'); } catch (_) { /* no-op */ }
    }

    function closeMobileInfoPanel() {
        try { document.body.classList.remove('conv-mobile-info-open'); } catch (_) { /* no-op */ }
    }

    function setupMobileInfoPanel() {
        try {
            const closeBtn = document.getElementById('btnCloseInfoMobile');
            if (closeBtn) closeBtn.addEventListener('click', closeMobileInfoPanel);
            // voltar ao desktop com a subview aberta nao pode prender o layout
            if (infoLayoutQuery.addEventListener) {
                infoLayoutQuery.addEventListener('change', (e) => {
                    if (!e.matches) closeMobileInfoPanel();
                });
            }
        } catch (_) { /* nunca quebra o chat */ }
    }
    // ─── fim CONV-MOBILE-RESPONSIVE-02 ───

    // ─── Init ───────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        if (!Auth.requireAuth()) return;

        loadUsers();
        setupEventListeners();
        setupConvNotificationControls();  // CONV-NOTIFICATIONS-01
        setupMobileLayout();              // CONV-MOBILE-PWA-01: lista primeiro
        setupMobileInfoPanel();           // CONV-MOBILE-RESPONSIVE-02
        loadTags();          // CONV-05
        restoreActiveInbox();   // PACOTE-B: default 'meus' (ou ultima escolha)
        loadCounts();
        loadConversations();

        // Poll for new messages every 5 seconds
        pollInterval = setInterval(async () => {
            loadCounts();          // badges + deteccao de notificacao
            // Acima do teto do backend nao da para reconsultar a janela em UMA
            // chamada; preservar o que esta na tela vale mais que atualizar,
            // e qualquer acao do usuario recarrega. Badges seguem vivos.
            if (loadedWindowSize <= MAX_PAGE_LIMIT) loadConversations('refresh');
            if (activeConversation) {
                const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}`);
                if (!resp || !resp.ok) return;
                const data = await resp.json();
                // CONV-NOTIFICATIONS-01: novas inbound da conversa aberta
                processChatNotifications(data, true);
                const oldCount = (activeConversation.messages || []).length;
                const newCount = (data.messages || []).length;
                // CONV-WINDOW-01: a janela fecha pela PASSAGEM DO TEMPO — as 24h
                // viram sem que nenhuma mensagem nova chegue. Comparar so a
                // contagem deixaria o composer aberto indefinidamente numa janela
                // ja fechada. Sem timer paralelo: o polling de 5s ja existia.
                const windowChanged =
                    activeConversation.service_window_open !== data.service_window_open;
                if (newCount !== oldCount || windowChanged) {
                    activeConversation = data;
                    renderChat();
                    renderLeadPanel();
                }
            }
        }, 5000);
    });

    // ─── Load Users ─────────────────────────────
    async function loadUsers() {
        const resp = await Auth.apiRequest('/api/conversations/users');
        if (!resp || !resp.ok) return;

        const data = await resp.json();
        usersCache = data.users || [];

        // Populate filter dropdown
        const filterSelect = document.getElementById('filterResponsavel');
        const atendenteSelect = document.getElementById('selectAtendente'); // CONV-07
        // Keep first two options (Todos / Agente IA)
        usersCache.forEach(u => {
            const opt = document.createElement('option');
            opt.value = u.id;
            opt.textContent = u.nome;
            filterSelect.appendChild(opt);
            if (atendenteSelect) {
                const opt2 = document.createElement('option');
                opt2.value = u.id;
                opt2.textContent = u.nome;   // textContent = seguro
                atendenteSelect.appendChild(opt2);
            }
        });

        // Populate responsavel select in panel
        populateResponsavelSelect();
    }

    function populateResponsavelSelect() {
        const select = document.getElementById('selectResponsavel');
        if (!select) return;
        select.innerHTML = '<option value="0">Agente IA</option>';
        usersCache.forEach(u => {
            const opt = document.createElement('option');
            opt.value = u.id;
            opt.textContent = u.nome;
            select.appendChild(opt);
        });
    }

    // ─── Event Listeners ────────────────────────
    function setupEventListeners() {
        // Logout
        document.getElementById('btnLogout').addEventListener('click', () => {
            Auth.logout();
        });

        // Search
        // PACOTE-B: busca vai ao SERVIDOR (com debounce), senao encontraria
        // apenas o que ja esta na pagina carregada.
        let searchTimer = null;
        document.getElementById('searchInput').addEventListener('input', (e) => {
            searchTerm = e.target.value;
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => loadConversations(), 300);
        });

        // PACOTE-B: seletor de inbox. Trocar de categoria refaz a BUSCA no
        // servidor (nao refiltra array local) e zera a paginacao.
        document.querySelectorAll('.conv-inbox-menu button[data-inbox]').forEach(btn => {
            btn.addEventListener('click', () => {
                setActiveInbox(btn.dataset.inbox);
                const sel = document.getElementById('inboxSelector');
                if (sel) sel.open = false;
                loadConversations();
            });
        });
        // Escape fecha o dropdown — ESCOPADO ao proprio <details>, sem
        // keydown global (invariante compartilhado dos pacotes anteriores).
        const inboxSel = document.getElementById('inboxSelector');
        if (inboxSel) {
            inboxSel.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && inboxSel.open) {
                    inboxSel.open = false;
                    const sum = inboxSel.querySelector('summary');
                    if (sum) sum.focus();
                }
            });
            document.addEventListener('click', (e) => {
                if (inboxSel.open && !inboxSel.contains(e.target)) inboxSel.open = false;
            });
        }

        // Responsavel filter
        document.getElementById('filterResponsavel').addEventListener('change', (e) => {
            activeResponsavelFilter = e.target.value;
            loadConversations();
        });

        // CONV-05: tag filter + aplicar/criar tag
        document.getElementById('filterTag').addEventListener('change', (e) => {
            activeTagFilter = e.target.value;
            loadConversations();
        });
        document.getElementById('selectAddTag').addEventListener('change', async (e) => {
            const tagId = Number(e.target.value);
            e.target.value = '';
            if (!activeConversation || !tagId) return;
            const resp = await Auth.apiRequest(
                `/api/conversations/${activeConversation.id}/tags/${tagId}`, { method: 'POST' });
            if (resp && resp.ok) {
                activeConversation.tags = await resp.json();
                renderConvTags();
                loadConversations();
            } else {
                showToast('Falha ao aplicar a tag.');
            }
        });
        // CONV-TAGS-UX-01: "+ Nova" abre o MODAL interno (nada de prompt nativo)
        document.getElementById('btnNewTag').addEventListener('click', openTagModal);
        document.getElementById('btnTagModalClose').addEventListener('click', closeTagModal);
        document.getElementById('btnTagModalCancel').addEventListener('click', closeTagModal);
        document.getElementById('tagModalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeTagModal();  // clique fora fecha
        });
        document.getElementById('btnTagModalCreate').addEventListener('click', createAndApplyTagFromModal);
        document.getElementById('tagModalName').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); createAndApplyTagFromModal(); }
        });

        // Responsavel selector in panel
        document.getElementById('selectResponsavel').addEventListener('change', async (e) => {
            if (!activeConversation) return;
            const newResp = parseInt(e.target.value) || 0;
            const resp = await Auth.apiRequest(
                `/api/conversations/${activeConversation.id}/responsavel?responsavel_id=${newResp}`,
                { method: 'PUT' }
            );
            if (resp && resp.ok) {
                const data = await resp.json();
                showToast(`Responsavel: ${data.responsavel_nome || 'Agente IA'}`);
                loadConversations();
            }
        });

        // --- TEMPLATE LOGIC ---
        // CONV-WINDOW-01: seletor de templates APROVADOS da Meta.
        // Um unico seletor serve os dois estados (janela aberta e fechada) — por
        // isso o dropdown mora fora de .chat-input no HTML.
        const btnShowTemplates = document.getElementById('btnShowTemplates');
        const btnCloseTemplates = document.getElementById('btnCloseTemplates');
        const templatesDropdown = document.getElementById('templatesDropdown');
        const templatesList = document.getElementById('templatesList');

        if (btnShowTemplates) {
            btnShowTemplates.addEventListener('click', openTemplatePicker);
            document.getElementById('btnOpenTemplatePicker').addEventListener('click', openTemplatePicker);
            btnCloseTemplates.addEventListener('click', closeTemplatePicker);
            document.addEventListener('click', (e) => {
                if (templatesDropdown.style.display === 'none') return;
                const inside = templatesDropdown.contains(e.target)
                    || btnShowTemplates.contains(e.target)
                    || document.getElementById('btnOpenTemplatePicker').contains(e.target);
                if (!inside) closeTemplatePicker();
            });
        }
        // --- END TEMPLATE LOGIC ---

        // Send message
        document.getElementById('btnSend').addEventListener('click', sendMessage);

        // CONV-VAR-01: gatilho do seletor de variaveis (secao logica mais abaixo)
        document.getElementById('btnVars').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleVarPalette();
        });
        document.addEventListener('click', (e) => {
            const palette = document.getElementById('varPalette');
            const button = document.getElementById('btnVars');
            if (varPaletteOpen && !palette.contains(e.target) && !button.contains(e.target)) {
                closeVarPalette();
            }
        });

        // CONV-VAR-01-HARD-01: previa (secao logica mais abaixo). Apenas
        // exibe — jamais envia.
        document.getElementById('btnPreview').addEventListener('click', openPreview);
        document.getElementById('previewModalClose').addEventListener('click', closePreview);
        document.getElementById('btnPreviewClose').addEventListener('click', closePreview);
        document.getElementById('previewModalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closePreview();
        });

        // CONV-06: assumir/liberar conversa
        document.getElementById('btnClaim').addEventListener('click', claimOrRelease);

        // CONV-07: atribuicao dirigida + notas internas
        document.getElementById('selectAtendente').addEventListener('change', (e) => {
            assignTo(e.target.value);
        });
        document.getElementById('btnAddNote').addEventListener('click', addNote);

        // CONV-03: anexo de midia
        document.getElementById('btnAttach').addEventListener('click', () => {
            if (!activeConversation) { showToast('Abra uma conversa primeiro'); return; }
            document.getElementById('mediaFileInput').click();
        });
        document.getElementById('mediaFileInput').addEventListener('change', async function () {
            const file = this.files && this.files[0];
            this.value = ''; // permite reanexar o mesmo arquivo
            if (file) await sendMediaFile(file);
        });

        // Textarea: Enter to send, Shift+Enter for newline.
        // CONV-HOTFIX-QUICK-REPLIES-01: a paleta "/" consome a tecla ANTES do
        // envio — selecionar mensagem rapida NUNCA envia automaticamente.
        document.getElementById('msgInput').addEventListener('keydown', (e) => {
            if (handleQrPaletteKeydown(e)) return;
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                // CONV-WINDOW-01: Enter e um caminho de envio independente do
                // #btnSend — desabilitar so o botao deixaria esta porta aberta.
                if (windowClosed()) { applyWindowState(activeConversation); return; }
                sendMessage();
            }
        });

        // CONV-HOTFIX-QUICK-REPLIES-01: gatilho/filtro da paleta — ligado
        // SOMENTE ao composer (#msgInput); outros campos nao abrem a paleta.
        document.getElementById('msgInput').addEventListener('input', updateQrPalette);
        document.addEventListener('click', (e) => {
            const palette = document.getElementById('qrPalette');
            const input = document.getElementById('msgInput');
            if (qrOpen && !palette.contains(e.target) && e.target !== input) {
                closeQrPalette(false);
            }
        });

        // Auto-resize textarea
        document.getElementById('msgInput').addEventListener('input', function () {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 120) + 'px';
        });

        // Close conversation
        document.getElementById('btnCloseConv').addEventListener('click', async () => {
            if (!activeConversation) return;
            const newStatus = activeConversation.status === 'encerrada' ? 'aberta' : 'encerrada';
            await Auth.apiRequest(`/api/conversations/${activeConversation.id}`, {
                method: 'PUT',
                body: JSON.stringify({ status: newStatus }),
            });
            showToast(newStatus === 'encerrada' ? 'Conversa encerrada' : 'Conversa reaberta');
            loadConversations();
            if (activeConversation) loadChat(activeConversation.id);
        });

        // Toggle info panel
        document.getElementById('btnToggleInfo').addEventListener('click', () => {
            // CONV-MOBILE-RESPONSIVE-02: em <=1200px o painel e uma subview
            // em tela cheia governada por classe (o display inline perderia
            // para o !important da media query)
            if (infoLayoutQuery.matches) {
                document.body.classList.toggle('conv-mobile-info-open');
                return;
            }
            const panel = document.getElementById('leadPanel');
            panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
        });

        // Link lead (auto-link via WhatsApp)
        document.getElementById('btnLinkLead').addEventListener('click', async () => {
            if (!activeConversation) return;

            const resp = await Auth.apiRequest(
                `/api/conversations/${activeConversation.id}/auto-link`,
                { method: 'POST' }
            );
            if (resp && resp.ok) {
                const data = await resp.json();
                showToast(`Vinculado ao Lead #${data.lead_id}`);
                loadChat(activeConversation.id);
            } else {
                showToast('Lead nao encontrado no CRM com este WhatsApp');
            }
        });

        // Toggle bot
        document.getElementById('btnToggleBot').addEventListener('click', async () => {
            if (!activeConversation) return;
            const newValue = !activeConversation.is_bot_active;
            await Auth.apiRequest(`/api/conversations/${activeConversation.id}`, {
                method: 'PUT',
                body: JSON.stringify({ is_bot_active: newValue }),
            });
            showToast(newValue ? 'Bot ativado' : 'Bot desativado');
            loadChat(activeConversation.id);
        });

        // Mobile back
        document.getElementById('mobileBack').addEventListener('click', () => {
            closeMobileInfoPanel(); // CONV-MOBILE-RESPONSIVE-02: voltar fecha a subview de info
            document.getElementById('chatActive').style.display = 'none';
            document.getElementById('chatEmpty').style.display = 'flex';
            document.getElementById('convSidebar').classList.add('open');
        });

        // Check URL params — open specific conversation
        const params = new URLSearchParams(window.location.search);

        const openId  = params.get('open');     // ?open=CONV_ID
        const leadId  = params.get('lead_id'); // ?lead_id=LEAD_ID (legado)
        const newLead = params.get('new_lead'); // ?new_lead=ID + new_wpp=NUM
        const newWpp  = params.get('new_wpp');
        const newNome = params.get('nome');

        if (openId) {
            setTimeout(() => loadChat(parseInt(openId)), 500);
        } else if (newLead && newWpp) {
            // Vindo do CRM: busca ou cria conversa e abre
            setTimeout(() => resolveAndOpenConversation(parseInt(newLead), newWpp, newNome), 600);
        } else if (leadId) {
            loadConversationByLead(parseInt(leadId));
        }
    }

    // ─── API Calls ──────────────────────────────
    // PACOTE-B: categoria ativa (default 'meus'; ultima escolha preservada no
    // localStorage ja usado pelo Auth — sem infraestrutura nova).
    function setActiveInbox(key) {
        if (!INBOX_KEYS.includes(key)) key = 'meus';
        activeInbox = key;
        loadedWindowSize = PAGE_SIZE;   // troca de categoria zera a paginacao
        try { localStorage.setItem(INBOX_STORAGE_KEY, key); } catch (e) { /* ignora */ }
        const label = document.getElementById('inboxCurrentLabel');
        if (label) label.textContent = INBOX_LABELS[key];
        document.querySelectorAll('.conv-inbox-menu button[data-inbox]').forEach(b => {
            b.setAttribute('aria-selected', String(b.dataset.inbox === key));
        });
        renderInboxCounts();
    }

    function restoreActiveInbox() {
        let saved = null;
        try { saved = localStorage.getItem(INBOX_STORAGE_KEY); } catch (e) { /* ignora */ }
        setActiveInbox(INBOX_KEYS.includes(saved) ? saved : 'meus');
    }

    function renderInboxCounts() {
        document.querySelectorAll('.conv-inbox-badge[data-count]').forEach(el => {
            const n = Number(inboxCounts[el.dataset.count] || 0);
            el.textContent = String(n);
            el.setAttribute('data-zero', n === 0 ? '1' : '0');
        });
        const cur = document.getElementById('inboxCurrentCount');
        if (cur) {
            const n = Number(inboxCounts[activeInbox] || 0);
            cur.textContent = String(n);
            cur.setAttribute('data-zero', n === 0 ? '1' : '0');
        }
    }

    // Contagens REAIS por categoria + dataset de notificacao. Uma falha aqui
    // NAO pode derrubar o inbox: a lista continua utilizavel sem badge.
    async function loadCounts() {
        try {
            const resp = await Auth.apiRequest('/api/conversations/counts');
            if (!resp || !resp.ok) return;
            const data = await resp.json();
            inboxCounts = data || {};
            renderInboxCounts();
            // CONV-NOTIFICATIONS-01 + PACOTE-B: a deteccao usa o mapa `unread`
            // do /counts (universo ABERTO inteiro), NAO a lista filtrada —
            // senao trocar de aba deixaria o operador cego para mensagens de
            // fora dela.
            processUnreadNotifications(data.unread || {});
        } catch (err) {
            console.warn('Counts ignorado (nao derruba o inbox):', err);
        }
    }

    // mode: undefined = busca NOVA (volta a pagina 1 e zera a janela)
    //       'append'      = proxima pagina ("Carregar mais"), amplia a janela
    //       'refresh'     = polling: reconsulta offset 0 ate loadedWindowSize
    //                       e SUBSTITUI a lista (nunca faz append, nunca
    //                       reseta para PAGE_SIZE)
    async function loadConversations(mode) {
        const isAppend = mode === 'append';
        const isRefresh = mode === 'refresh';
        const seq = ++listRequestSeq;
        if (!isAppend && !isRefresh) loadedWindowSize = PAGE_SIZE;
        const limit = isRefresh ? Math.min(loadedWindowSize, MAX_PAGE_LIMIT) : PAGE_SIZE;
        const params = new URLSearchParams({
            inbox: activeInbox,
            limit: String(limit),
            // offset deriva da janela ja carregada — sem estado paralelo.
            offset: String(isAppend ? conversations.length : 0),
        });
        if (searchTerm) params.set('search', searchTerm);
        if (activeResponsavelFilter !== '') params.set('responsavel_id', activeResponsavelFilter);
        if (activeTagFilter !== '') params.set('tag_id', String(Number(activeTagFilter)));

        let resp = null;
        try {
            resp = await Auth.apiRequest(`/api/conversations?${params.toString()}`);
        } catch (err) {
            resp = null;
        }
        // Resposta obsoleta (o usuario ja trocou de categoria/filtro): descarta.
        if (seq !== listRequestSeq) return;

        if (!resp || !resp.ok) {
            listError = true;
            renderConversationList();
            return;
        }
        listError = false;
        const data = await resp.json();
        if (seq !== listRequestSeq) return;
        const page = data.conversations || [];
        conversations = isAppend ? conversations.concat(page) : page;
        listTotal = Number(data.total || 0);
        // A janela so CRESCE com append/busca nova; o refresh a mantem.
        if (!isRefresh) loadedWindowSize = Math.max(PAGE_SIZE, conversations.length);
        renderConversationList();
    }

    async function loadChat(conversationId) {
        const resp = await Auth.apiRequest(`/api/conversations/${conversationId}`);
        if (!resp || !resp.ok) return;

        activeConversation = await resp.json();
        // CONV-NOTIFICATIONS-01: abrir a conversa baselina o historico
        // (silencioso) e "da o visto" no contador global.
        processChatNotifications(activeConversation, false);
        ackConvNotifications();
        renderChat();
        renderLeadPanel();

        // Show chat, hide empty state
        document.getElementById('chatEmpty').style.display = 'none';
        const chatActive = document.getElementById('chatActive');
        chatActive.style.display = 'flex';
        chatActive.style.flexDirection = 'column';
        document.getElementById('leadPanel').style.display = 'flex';

        // CONV-MOBILE-PWA-01: no mobile, abrir a conversa fecha o drawer da
        // lista (chat em tela cheia); #mobileBack reabre (handler existente)
        closeMobileDrawerForChat();
        // CONV-MOBILE-RESPONSIVE-02: trocar de conversa reseta a subview de info
        closeMobileInfoPanel();

        // Highlight in list
        document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
        const activeEl = document.querySelector(`.conv-item[data-id="${conversationId}"]`);
        if (activeEl) activeEl.classList.add('active');
    }

    async function loadConversationByLead(leadId) {
        try {
            const resp = await Auth.apiRequest(`/api/conversations/by-lead/${leadId}`);
            if (resp && resp.ok) {
                const conv = await resp.json();
                setTimeout(() => loadChat(conv.id), 500);
            } else {
                console.log('No conversation found for lead', leadId);
            }
        } catch (e) {
            console.log('No conversation found for lead', leadId);
        }
    }

    /**
     * Vindo do CRM: busca conversa existente pelo lead_id.
     * Se não encontrar, chama /initiate (mesmo domínio, sem CORS)
     * para criar a conversa e abre direto.
     */
    async function resolveAndOpenConversation(leadId, whatsapp, nome) {
        // 1. Tenta pelo lead_id
        try {
            const r = await Auth.apiRequest(`/api/conversations/by-lead/${leadId}`);
            if (r && r.ok) {
                const conv = await r.json();
                loadChat(conv.id);
                return;
            }
        } catch (_) {}

        // 2. Tenta pelo número (pode já existir sem vinculação ao lead)
        try {
            const byWpp = conversations.find(c => c.whatsapp === whatsapp);
            if (byWpp) {
                loadChat(byWpp.id);
                return;
            }
        } catch (_) {}

        // 3. Não existe — cria conversa vazia via /initiate
        showToast(`Abrindo conversa com ${nome || whatsapp}...`);
        try {
            const r = await Auth.apiRequest('/api/conversations/initiate', {
                method: 'POST',
                body: JSON.stringify({
                    whatsapp: whatsapp,
                    nome: nome || whatsapp,
                    lead_id: leadId,
                })
            });
            if (r && r.ok) {
                const data = await r.json();
                await loadConversations(); // atualiza lista
                loadChat(data.conversation_id);
            } else {
                showToast('Não foi possível criar a conversa.');
            }
        } catch (e) {
            showToast('Erro ao conectar com o servidor.');
        }
    }

    /**
     * Fallback: busca na lista pelo número.
     */
    function openNewContactPanel(whatsapp, leadId, nome) {
        const existing = conversations.find(c => c.whatsapp === whatsapp);
        if (existing) {
            loadChat(existing.id);
            return;
        }
        const searchInput = document.getElementById('searchInput');
        if (searchInput) {
            searchInput.value = whatsapp;
            searchInput.dispatchEvent(new Event('input'));
        }
        showToast(`Buscando conversa com ${nome || whatsapp}...`);
    }

    // ─── CONV-WINDOW-01: janela de 24h da Meta ─────────────────────────
    // O valor vem SEMPRE do backend (`service_window_open` em ConversationResponse).
    // O JS nunca recalcula 24 horas — se recalculasse, frontend e backend
    // discordariam no minuto da virada.

    let tplSending = false;   // guard de duplo clique (UI)

    /**
     * Aplica o estado da janela ao composer. Esconder o composer NAO basta:
     * Enter, o input de arquivo e os botoes de reenvio continuariam vivos.
     * Cada mecanismo free-form e desligado explicitamente.
     */
    function applyWindowState(conv) {
        const open = !!(conv && conv.service_window_open);
        const composer = document.getElementById('chatComposer');
        const closedBox = document.getElementById('windowClosedBox');
        if (!composer || !closedBox) return;

        composer.style.display = open ? '' : 'none';
        closedBox.style.display = open ? 'none' : '';

        // Redundante com o display:none (o backend e a autoridade de verdade),
        // mas impede submit por atalho/foco preso enquanto o no existe.
        const input = document.getElementById('msgInput');
        const btnSend = document.getElementById('btnSend');
        const btnAttach = document.getElementById('btnAttach');
        if (input) input.disabled = !open;
        if (btnSend) btnSend.disabled = !open;
        if (btnAttach) btnAttach.disabled = !open;

        if (!open) {
            closeQrPalette(false);
            closeVarPalette();
        } else {
            document.getElementById('windowClosedText').textContent =
                'O cliente não envia uma mensagem há mais de 24 horas. '
                + 'Para retomar o atendimento, envie um template aprovado.';
        }
    }

    /** Janela fechada => nenhum caminho free-form pode disparar. */
    function windowClosed() {
        return !!(activeConversation && activeConversation.service_window_open === false);
    }

    /**
     * 409 WINDOW_CLOSED: a janela fechou entre o render e o POST. O backend
     * recusou ANTES de tocar a Meta e sem persistir Message. Aqui so refletimos:
     * marca a conversa como fechada e troca o composer na hora.
     */
    async function handleWindowClosed(detail) {
        if (activeConversation) activeConversation.service_window_open = false;
        applyWindowState(activeConversation);
        showToast((detail && detail.message) || 'Janela de 24h encerrada. Envie um template aprovado.');
        loadConversations();
    }

    /** Extrai {code, message} do corpo de erro sem quebrar em detail string. */
    async function readErrorDetail(resp) {
        try {
            const body = await resp.json();
            const d = body && body.detail;
            if (d && typeof d === 'object') return d;
            if (typeof d === 'string') return { message: d };
        } catch (_) { /* sem corpo JSON */ }
        return {};
    }

    function closeTemplatePicker() {
        document.getElementById('templatesDropdown').style.display = 'none';
    }

    async function openTemplatePicker(e) {
        if (e) e.stopPropagation();
        if (!activeConversation) { showToast('Abra uma conversa primeiro'); return; }

        const dropdown = document.getElementById('templatesDropdown');
        const list = document.getElementById('templatesList');
        dropdown.style.display = 'block';
        list.textContent = '';
        list.appendChild(noticeEl('Carregando templates aprovados...'));

        const resp = await Auth.apiRequest('/api/templates/meta/approved');
        // Falha ao listar NUNCA libera texto livre: o composer segue como esta.
        if (!resp || !resp.ok) {
            const d = resp ? await readErrorDetail(resp) : {};
            list.textContent = '';
            list.appendChild(noticeEl(d.message || 'Não foi possível carregar os templates.', true));
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'btn-window-template';
            retry.textContent = 'Tentar novamente';
            retry.addEventListener('click', (ev) => { ev.stopPropagation(); openTemplatePicker(); });
            list.appendChild(retry);
            return;
        }

        const data = await resp.json();
        const templates = data.templates || [];
        list.textContent = '';
        if (!templates.length) {
            list.appendChild(noticeEl('Nenhum template aprovado na conta Meta.'));
            return;
        }
        templates.forEach(t => list.appendChild(templateItemEl(t)));
    }

    function noticeEl(text, isError) {
        const el = document.createElement('div');
        el.style.cssText = 'padding:10px; text-align:center; font-size:12px; color:'
            + (isError ? 'var(--error)' : 'var(--dark-400)') + ';';
        el.textContent = text;
        return el;
    }

    /**
     * Um item do seletor. Template com estrutura ainda nao suportada aparece
     * DESABILITADO com o motivo visivel — nunca some em silencio, nunca vira
     * payload adivinhado.
     */
    function templateItemEl(t) {
        const el = document.createElement('div');
        el.className = 'tpl-item' + (t.supported ? ' selectable' : ' disabled');

        const nameRow = document.createElement('div');
        nameRow.style.cssText = 'display:flex; align-items:center; gap:6px; margin-bottom:2px;';
        const name = document.createElement('span');
        name.className = 'tpl-name';
        name.textContent = t.name;
        const lang = document.createElement('span');
        lang.className = 'tpl-lang';
        lang.textContent = t.language;
        nameRow.appendChild(name);
        nameRow.appendChild(lang);
        el.appendChild(nameRow);

        if (t.header_text) {
            const h = document.createElement('div');
            h.className = 'tpl-body';
            h.style.fontWeight = '600';
            h.textContent = t.header_text;
            el.appendChild(h);
        }
        const body = document.createElement('div');
        body.className = 'tpl-body';
        body.textContent = t.body_text;
        el.appendChild(body);

        if (!t.supported) {
            const reason = document.createElement('div');
            reason.className = 'tpl-reason';
            reason.textContent = 'Este template possui componentes ainda não suportados: '
                + (t.unsupported_reason || 'estrutura desconhecida') + '.';
            el.appendChild(reason);
            return el;
        }

        el.addEventListener('click', (ev) => {
            ev.stopPropagation();
            openTemplateForm(t);
        });
        return el;
    }

    /** Formulario de parametros + preview. A aridade vem da Meta, via backend. */
    function openTemplateForm(t) {
        const list = document.getElementById('templatesList');
        list.textContent = '';

        const head = document.createElement('div');
        head.className = 'tpl-item';
        const name = document.createElement('span');
        name.className = 'tpl-name';
        name.textContent = t.name;
        const lang = document.createElement('span');
        lang.className = 'tpl-lang';
        lang.style.marginLeft = '6px';
        lang.textContent = t.language;
        head.appendChild(name);
        head.appendChild(lang);
        list.appendChild(head);

        const preview = document.createElement('div');
        preview.className = 'tpl-body';
        preview.style.cssText = 'background:var(--dark-100); border-radius:6px; padding:8px; margin:8px 0;';

        const inputs = [];
        const form = document.createElement('div');
        form.style.padding = '0 10px';
        for (let i = 1; i <= t.body_params; i++) {
            const label = document.createElement('label');
            label.style.cssText = 'font-size:11px; color:var(--dark-500); display:block; margin-top:6px;';
            label.textContent = '{{' + i + '}}';
            const inp = document.createElement('input');
            inp.type = 'text';
            inp.className = 'tpl-param-input';
            inp.addEventListener('input', updatePreview);
            label.appendChild(inp);
            form.appendChild(label);
            inputs.push(inp);
        }

        function values() { return inputs.map(i => i.value); }
        function updatePreview() {
            const vals = values();
            preview.textContent = t.body_text.replace(/\{\{(\d+)\}\}/g, (m, n) => {
                const v = vals[Number(n) - 1];
                return v ? v : m;
            });
            send.disabled = tplSending || vals.some(v => !v.trim());
        }

        const send = document.createElement('button');
        send.type = 'button';
        send.className = 'btn-window-template';
        send.style.margin = '8px 10px';
        send.textContent = 'Enviar template';
        send.addEventListener('click', (ev) => {
            ev.stopPropagation();
            sendTemplate(t, values(), send);
        });

        list.appendChild(form);
        list.appendChild(preview);
        list.appendChild(send);
        updatePreview();
    }

    /**
     * Envia o template. Guard de duplo clique e de UI (botao disabled + flag):
     * idempotencia contra duas requisicoes INDEPENDENTES fica fora deste pacote.
     */
    async function sendTemplate(t, params, btn) {
        if (tplSending || !activeConversation) return;
        tplSending = true;
        btn.disabled = true;
        const label = btn.textContent;
        btn.textContent = 'Enviando...';

        try {
            const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/messages`, {
                method: 'POST',
                body: JSON.stringify({
                    content: t.body_text,
                    msg_type: 'template',
                    template_name: t.name,
                    template_language: t.language,
                    template_params: params,
                }),
            });

            if (resp && resp.ok) {
                closeTemplatePicker();
                // Template enviado NAO reabre a janela: so uma inbound do cliente
                // reabre. O composer continua bloqueado por construcao — o backend
                // nao tocou em last_customer_msg_at.
                document.getElementById('windowClosedText').textContent =
                    'Template enviado. Aguardando uma resposta do cliente para reabrir a janela de atendimento.';
                showToast('Template enviado.');
                loadChat(activeConversation.id);
                loadConversations();
            } else {
                // Falha NAO cria mensagem falsa e NAO libera texto: so informa e
                // deixa tentar de novo.
                const d = resp ? await readErrorDetail(resp) : {};
                showToast(d.message || 'Não foi possível enviar o template. Tente novamente.');
            }
        } finally {
            tplSending = false;
            btn.disabled = false;
            btn.textContent = label;
        }
    }

    async function sendMessage() {
        const input = document.getElementById('msgInput');
        const content = input.value.trim();
        if (!content || !activeConversation) return;
        if (windowClosed()) { applyWindowState(activeConversation); return; }

        input.value = '';
        input.style.height = 'auto';

        // Optimistic UI: add message immediately
        appendMessage({
            direction: 'outbound',
            content: content,
            msg_type: 'text',
            status: 'sending',
            created_at: new Date().toISOString(),
        });

        const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/messages`, {
            method: 'POST',
            body: JSON.stringify({ content, msg_type: 'text' }),
        });

        if (resp && resp.ok) {
            loadChat(activeConversation.id);
            loadConversations();
        } else {
            // (CONV-08b: mensagens 'failed' persistidas exibem botao de reenvio no chat)
            // CONV-08: o envio falhou. O backend NAO marca a mensagem como 'sent' e a
            // persiste como 'failed'. Recarrega o chat para exibir o status de falha
            // (X vermelho) e avisa o operador com uma mensagem segura (sem segredos).
            const d = resp ? await readErrorDetail(resp) : {};
            // CONV-WINDOW-01 (race): a janela fechou entre o render e o POST.
            // Devolve o texto ao composer antes de trocar a tela — o operador
            // nao perde o que escreveu — e passa a exibir o bloco de template.
            if (resp && resp.status === 409 && d.code === 'WINDOW_CLOSED') {
                input.value = content;
                await handleWindowClosed(d);
                loadChat(activeConversation.id);
                return;
            }
            let detail = d.message || 'Falha ao enviar a mensagem. Tente novamente.';
            // CONV-VAR-01: 422 = variavel nao resolvida. O backend BLOQUEOU o
            // envio e nao persistiu nada, entao devolvemos o texto original ao
            // composer para o vendedor corrigir o token sem reescrever tudo.
            if (resp && resp.status === 422 && !input.value) {
                input.value = content;
                input.focus();
            }
            showToast(detail);
            loadChat(activeConversation.id);
        }
    }

    // ─── CONV-HOTFIX-QUICK-REPLIES-01: paleta de mensagens rapidas ("/") ───
    // Fonte: backend PRE-EXISTENTE /api/quick-replies (gerenciadas em /settings).
    // A paleta abre APENAS quando o valor do composer COMECA com "/" — barra
    // digitada no meio do texto e sempre literal. Esc fecha e suprime a
    // reabertura ate o "/" inicial sair do campo (permite manter "/" literal).
    // Itens renderizados com createElement/textContent (conteudo controlado
    // por usuarios via settings — nunca interpolado como HTML).
    let qrOpen = false;
    let qrDismissed = false;
    let qrLoaded = false;
    let qrAll = [];        // cache da ultima carga (apenas ativas)
    let qrItems = [];      // itens filtrados exibidos
    let qrIndex = 0;
    let qrFetchSeq = 0;

    async function fetchQuickReplies() {
        const seq = ++qrFetchSeq;
        const resp = await Auth.apiRequest('/api/quick-replies');
        if (!resp || !resp.ok || seq !== qrFetchSeq) return;
        const data = await resp.json();
        qrAll = data.quick_replies || [];
        qrLoaded = true;
        if (qrOpen) renderQrPalette();
    }

    function updateQrPalette() {
        const value = document.getElementById('msgInput').value;
        if (!value.startsWith('/')) {
            qrDismissed = false;   // "/" inicial saiu do campo: proximo "/" reabre
            if (qrOpen) closeQrPalette(false);
            return;
        }
        if (qrDismissed) return;   // Esc: "/" segue literal ate limpar o campo
        if (!qrOpen) {
            qrOpen = true;
            qrIndex = 0;
            fetchQuickReplies();   // recarrega a cada abertura (1 request, pega edicoes do settings)
        }
        renderQrPalette();
    }

    function renderQrPalette() {
        const palette = document.getElementById('qrPalette');
        const q = document.getElementById('msgInput').value.slice(1).trim().toLowerCase();
        qrItems = qrAll.filter(r =>
            !q ||
            (r.shortcut || '').toLowerCase().includes(q) ||
            (r.title || '').toLowerCase().includes(q) ||
            (r.content || '').toLowerCase().includes(q)
        );
        if (qrIndex >= qrItems.length) qrIndex = 0;

        palette.replaceChildren();
        const header = document.createElement('div');
        header.className = 'qr-palette-header';
        header.textContent = 'Mensagens rápidas';
        palette.appendChild(header);

        if (qrItems.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'qr-palette-empty';
            empty.textContent = !qrLoaded
                ? 'Carregando…'
                : (qrAll.length === 0
                    ? 'Nenhuma mensagem rápida cadastrada (crie em Configurações).'
                    : 'Nenhuma mensagem rápida encontrada.');
            palette.appendChild(empty);
        } else {
            qrItems.forEach((r, i) => {
                const item = document.createElement('div');
                item.className = 'qr-palette-item' + (i === qrIndex ? ' active' : '');
                item.setAttribute('role', 'option');
                item.setAttribute('aria-selected', i === qrIndex ? 'true' : 'false');
                const shortcut = document.createElement('div');
                shortcut.className = 'qr-shortcut';
                shortcut.textContent = r.shortcut || '';
                const title = document.createElement('div');
                title.className = 'qr-title';
                title.textContent = r.title || '';
                const preview = document.createElement('div');
                preview.className = 'qr-preview';
                preview.textContent = r.content || '';
                item.appendChild(shortcut);
                item.appendChild(title);
                item.appendChild(preview);
                item.addEventListener('click', () => selectQuickReply(i));
                palette.appendChild(item);
            });
        }
        palette.style.display = 'block';
    }

    function closeQrPalette(dismissed) {
        qrOpen = false;
        qrDismissed = !!dismissed;
        document.getElementById('qrPalette').style.display = 'none';
    }

    function selectQuickReply(i) {
        const r = qrItems[i];
        if (!r) return;
        const input = document.getElementById('msgInput');
        // Insere o texto no composer — NAO envia: o operador revisa/edita
        // e envia manualmente (btnSend ou Enter).
        input.value = r.content || '';
        closeQrPalette(false);
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
        // auto-resize (mesma regra do handler de input)
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    function moveQrHighlight(delta) {
        if (qrItems.length === 0) return;
        qrIndex = (qrIndex + delta + qrItems.length) % qrItems.length;
        renderQrPalette();
        const active = document.querySelector('#qrPalette .qr-palette-item.active');
        if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }

    // Retorna true se a paleta consumiu a tecla (o caller NAO envia).
    function handleQrPaletteKeydown(e) {
        // "/" digitado com o campo vazio e sempre um NOVO gatilho
        // (limpa o dismiss de um Esc anterior, mesmo apos envio/limpeza)
        if (e.key === '/' && e.target.value === '') qrDismissed = false;
        if (!qrOpen) return false;
        if (e.key === 'ArrowDown') { e.preventDefault(); moveQrHighlight(1); return true; }
        if (e.key === 'ArrowUp') { e.preventDefault(); moveQrHighlight(-1); return true; }
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            closeQrPalette(true);  // o "/" ja digitado vira texto literal
            return true;
        }
        if (e.key === 'Enter' && !e.shiftKey && qrItems.length > 0) {
            e.preventDefault();
            selectQuickReply(qrIndex);
            return true;
        }
        return false;
    }

    // ─── CONV-07: Atribuicao dirigida + notas ────
    async function assignTo(userId) {
        if (!activeConversation) return;
        const id = Number(userId);
        const url = id === 0
            ? `/api/conversations/${activeConversation.id}/release`
            : `/api/conversations/${activeConversation.id}/assign`;
        const opts = id === 0
            ? { method: 'POST' }
            : { method: 'POST', body: JSON.stringify({ user_id: id }) };
        const resp = await Auth.apiRequest(url, opts);
        if (resp && resp.ok) {
            const updated = await resp.json();
            activeConversation.atendente_id = updated.atendente_id;
            updateClaimButton(activeConversation);
            showToast(id === 0 ? 'Conversa devolvida à fila' : 'Conversa atribuída');
            loadConversations();
        } else {
            let detail = 'Falha ao atribuir.';
            try { const e = await resp.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
            showToast(detail);
        }
    }

    // Notas internas: conteudo e autor SEMPRE escapados; nunca vao ao WhatsApp.
    async function loadNotes() {
        const box = document.getElementById('notesList');
        if (!activeConversation) { box.innerHTML = ''; return; }
        const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/notes`);
        if (!resp || !resp.ok) { box.innerHTML = ''; return; }
        const notes = await resp.json();
        const me = Auth.getUser() || {};
        box.innerHTML = notes.length ? notes.map(n => {
            const when = new Date(n.created_at).toLocaleString('pt-BR',
                { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
            const del = n.user_id === me.id
                ? ` <span onclick="window._deleteNote(${Number(n.id)})" style="cursor:pointer; color:var(--error); font-weight:700;">&times;</span>`
                : '';
            return `<div style="background:#FEF9C3; border:1px solid #FDE047; border-radius:6px; padding:6px 8px; font-size:11px;">
                <div style="color:var(--dark-400); font-size:10px;">${escapeHtml(n.user_nome || 'Equipe')} · ${when}${del}</div>
                <div style="white-space:pre-wrap;">${escapeHtml(n.content)}</div>
            </div>`;
        }).join('') : '<span style="font-size:11px; color:var(--dark-400);">Sem notas</span>';
    }

    window._deleteNote = async function (noteId) {
        if (!activeConversation) return;
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/notes/${Number(noteId)}`, { method: 'DELETE' });
        if (resp && (resp.ok || resp.status === 204)) loadNotes();
        else showToast('Falha ao remover a nota.');
    };

    async function addNote() {
        if (!activeConversation) return;
        const input = document.getElementById('noteInput');
        const content = (input.value || '').trim();
        if (!content) return;
        const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/notes`, {
            method: 'POST',
            body: JSON.stringify({ content: content }),
        });
        if (resp && resp.ok) {
            input.value = '';
            loadNotes();
        } else {
            showToast('Falha ao salvar a nota.');
        }
    }

    // ─── CONV-TAGS-UX-01: modal de gestao de tags ─────────────────────────
    // CRM e a fonte de verdade para conversa VINCULADA (lead_id>0): aplicar/
    // remover/criar aqui replica no lead (endpoints do CONV-TAGS-SYNC-01);
    // sem lead, as tags sao LOCAIS e o modal avisa — nunca finge sync.
    function setTagModalStatus(text, isError) {
        const el = document.getElementById('tagModalStatus');
        el.textContent = text || '';
        el.className = 'tag-modal-status' + (isError ? ' error' : '');
    }

    function openTagModal() {
        if (!activeConversation) { showToast('Abra uma conversa primeiro'); return; }
        document.getElementById('tagModalOverlay').classList.add('open');
        setTagModalStatus('');
        document.getElementById('tagModalName').value = '';
        renderTagModal();
    }

    function closeTagModal() {
        document.getElementById('tagModalOverlay').classList.remove('open');
    }

    function renderTagModal() {
        if (!activeConversation) return;
        const linked = Number(activeConversation.lead_id) > 0;
        document.getElementById('tagModalTitle').textContent =
            linked ? 'Gerenciar tags do lead' : 'Gerenciar tags da conversa';
        document.getElementById('tagModalNotice').style.display = linked ? 'none' : 'block';

        const applied = activeConversation.tags || [];
        const appliedIds = new Set(applied.map(t => t.id));
        const appliedBox = document.getElementById('tagModalApplied');
        appliedBox.innerHTML = applied.length
            ? applied.map(t =>
                `<span class="tag-chip" style="border-color:${safeTagColor(t.cor)};">` +
                `<span class="chip-dot" style="background:${safeTagColor(t.cor)};"></span>` +
                `${escapeHtml(t.nome)}` +
                ` <span class="chip-x" onclick="window._modalRemoveTag(${Number(t.id)})" title="Remover">&times;</span></span>`
            ).join('')
            : '<span class="tag-modal-empty">Nenhuma tag aplicada</span>';

        const available = (allTags || []).filter(t => !appliedIds.has(t.id));
        const availBox = document.getElementById('tagModalAvailable');
        availBox.innerHTML = available.length
            ? available.map(t =>
                `<span class="tag-chip clickable" onclick="window._modalApplyTag(${Number(t.id)})" title="Aplicar tag">` +
                `<span class="chip-dot" style="background:${safeTagColor(t.cor)};"></span>` +
                `${escapeHtml(t.nome)}</span>`
            ).join('')
            : '<span class="tag-modal-empty">Todas as tags já estão aplicadas</span>';
    }

    async function refreshAfterTagChange(tagsFromApi) {
        activeConversation.tags = tagsFromApi;
        renderConvTags();
        renderTagModal();
        loadConversations();
    }

    window._modalApplyTag = async function (tagId) {
        if (!activeConversation) return;
        setTagModalStatus('Aplicando tag...');
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/tags/${Number(tagId)}`, { method: 'POST' });
        if (resp && resp.ok) {
            await refreshAfterTagChange(await resp.json());
            setTagModalStatus('');
            showToast('Tag aplicada');
        } else {
            setTagModalStatus('Não foi possível aplicar a tag. Tente novamente.', true);
        }
    };

    window._modalRemoveTag = async function (tagId) {
        if (!activeConversation) return;
        setTagModalStatus('Removendo tag...');
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/tags/${Number(tagId)}`, { method: 'DELETE' });
        if (resp && resp.ok) {
            await refreshAfterTagChange(await resp.json());
            setTagModalStatus('');
            showToast('Tag removida');
        } else {
            setTagModalStatus('Não foi possível remover a tag. Tente novamente.', true);
        }
    };

    async function createAndApplyTagFromModal() {
        if (!activeConversation) return;
        const nameInput = document.getElementById('tagModalName');
        const nome = (nameInput.value || '').trim();
        const cor = document.getElementById('tagModalColor').value || '#3B82F6';
        if (!nome) { setTagModalStatus('Digite o nome da tag.', true); return; }

        const btn = document.getElementById('btnTagModalCreate');
        btn.disabled = true;
        setTagModalStatus('Criando tag...');
        try {
            let tagId = null;
            const resp = await Auth.apiRequest('/api/tags', {
                method: 'POST',
                body: JSON.stringify({ nome: nome, cor: cor }),
            });
            if (resp && resp.ok) {
                tagId = (await resp.json()).id;
            } else if (resp && resp.status === 409) {
                // ja existe com esse nome -> reusa (sem duplicar) e aplica
                await loadTags();
                const existing = (allTags || []).find(
                    t => t.nome.toLowerCase() === nome.toLowerCase());
                if (existing) tagId = existing.id;
            }
            if (!tagId) {
                setTagModalStatus('Não foi possível criar a tag. Verifique o nome e tente novamente.', true);
                return;
            }
            await loadTags();  // catalogo/filtro da sidebar atualizados
            await window._modalApplyTag(tagId);  // criar SEMPRE aplica na conversa atual
            nameInput.value = '';
        } finally {
            btn.disabled = false;
        }
    }

    // ─── CONV-06: Fila (assumir/liberar) ─────────
    function updateClaimButton(conv) {
        const btn = document.getElementById('btnClaim');
        if (!btn) return;
        const me = Auth.getUser() || {};
        if (!isOpenStatus(conv.status)) {
            btn.style.display = 'none';
            return;
        }
        btn.style.display = 'inline-block';
        btn.disabled = false;
        if (!conv.atendente_id) {
            btn.textContent = 'Assumir';
            btn.dataset.action = 'claim';
        } else if (conv.atendente_id === me.id) {
            btn.textContent = 'Liberar';
            btn.dataset.action = 'release';
        } else {
            btn.textContent = 'Em atendimento';
            btn.dataset.action = '';
            btn.disabled = true;
        }
    }

    async function claimOrRelease() {
        if (!activeConversation) return;
        const btn = document.getElementById('btnClaim');
        const action = btn.dataset.action;
        if (!action) return;
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/${action}`, { method: 'POST' });
        if (resp && resp.ok) {
            const updated = await resp.json();
            activeConversation.atendente_id = updated.atendente_id;
            updateClaimButton(activeConversation);
            showToast(action === 'claim' ? 'Conversa assumida' : 'Conversa devolvida à fila');
            loadConversations();
        } else {
            let detail = 'Falha na operação.';
            try { const e = await resp.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
            showToast(detail);
            loadChat(activeConversation.id); // re-sincroniza (outro atendente pode ter assumido)
        }
    }

    // ─── CONV-05: Tags ───────────────────────────
    // Cor SEMPRE revalidada no cliente antes de ir para style (defesa em
    // profundidade — o backend ja valida ^#hex6$); nome SEMPRE via escapeHtml.
    function safeTagColor(cor) {
        return /^#[0-9A-Fa-f]{6}$/.test(cor || '') ? cor : '#999999';
    }

    function tagChipHtml(t, removable) {
        const remove = removable
            ? ` <span onclick="window._removeTag(${Number(t.id)})" style="cursor:pointer; font-weight:700;">&times;</span>`
            : '';
        return `<span style="background:${safeTagColor(t.cor)}22; border:1px solid ${safeTagColor(t.cor)}; color:var(--dark-600); border-radius:10px; font-size:10px; padding:1px 8px; white-space:nowrap;">${escapeHtml(t.nome)}${remove}</span>`;
    }

    async function loadTags() {
        const resp = await Auth.apiRequest('/api/tags');
        if (!resp || !resp.ok) return;
        allTags = await resp.json();

        const filterSel = document.getElementById('filterTag');
        const current = filterSel.value;
        filterSel.innerHTML = '<option value="">Todas as tags</option>';
        const addSel = document.getElementById('selectAddTag');
        addSel.innerHTML = '<option value="">Aplicar tag...</option>';
        allTags.forEach(t => {
            const o1 = document.createElement('option');
            o1.value = t.id;
            o1.textContent = t.nome;   // textContent = seguro
            filterSel.appendChild(o1);
            const o2 = document.createElement('option');
            o2.value = t.id;
            o2.textContent = t.nome;
            addSel.appendChild(o2);
        });
        filterSel.value = current;
    }

    function renderConvTags() {
        const box = document.getElementById('convTagChips');
        if (!activeConversation) { box.innerHTML = ''; return; }
        const tags = activeConversation.tags || [];
        box.innerHTML = tags.length
            ? tags.map(t => tagChipHtml(t, true)).join('')
            : '<span style="font-size:11px; color:var(--dark-400);">Sem tags</span>';
    }

    window._removeTag = async function (tagId) {
        if (!activeConversation) return;
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/tags/${Number(tagId)}`, { method: 'DELETE' });
        if (resp && resp.ok) {
            activeConversation.tags = await resp.json();
            renderConvTags();
            loadConversations();
        } else {
            showToast('Falha ao remover a tag.');
        }
    };

    // CONV-04: busca o blob de um asset (baixa da Meta sob demanda). Helper
    // unico usado por player/imagem/video/download — toast seguro em falha.
    async function fetchMediaBlob(assetId) {
        const id = Number(assetId);
        let resp = await Auth.apiRequest(`/api/media/${id}`);
        if (resp && resp.status === 409) {
            const f = await Auth.apiRequest(`/api/media/${id}/fetch`, { method: 'POST' });
            if (!f || !f.ok) {
                let detail = 'Falha ao baixar a mídia.';
                try { const e = await f.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
                showToast(detail);
                return null;
            }
            resp = await Auth.apiRequest(`/api/media/${id}`);
        }
        if (!resp || !resp.ok) { showToast('Mídia indisponível.'); return null; }
        return await resp.blob();
    }

    // CONV-04: render inline de imagem/video via blob autenticado
    window._showInlineMedia = async function (assetId, kind, btn) {
        if (btn) { btn.disabled = true; btn.textContent = 'Carregando...'; }
        const blob = await fetchMediaBlob(assetId);
        if (!blob) {
            if (btn) { btn.disabled = false; btn.textContent = 'Tentar novamente'; }
            return;
        }
        const url = URL.createObjectURL(blob);
        let el;
        if (kind === 'image') {
            el = document.createElement('img');
            el.src = url;
            el.style.maxWidth = '100%';
            el.style.borderRadius = '8px';
            el.style.cursor = 'zoom-in';
            el.onclick = () => window.open(url, '_blank');
        } else {
            el = document.createElement('video');
            el.controls = true;
            el.src = url;
            el.style.maxWidth = '260px';
            el.style.borderRadius = '8px';
        }
        if (btn && btn.parentNode) btn.parentNode.replaceChild(el, btn);
    };

    // CONV-04: download de documento com o filename original (via data-attribute
    // escapado — nunca interpolado em onclick)
    window._downloadMedia = async function (assetId, btn) {
        if (btn) { btn.disabled = true; }
        const blob = await fetchMediaBlob(assetId);
        if (btn) { btn.disabled = false; }
        if (!blob) return;
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = (btn && btn.dataset && btn.dataset.fn) || 'documento';
        document.body.appendChild(a);
        a.click();
        a.remove();
    };

    // CONV-03: envio de midia por upload (multipart). Nao usa Auth.apiRequest
    // porque ele fixa Content-Type: application/json — FormData exige que o
    // browser defina o boundary sozinho. O Bearer vai manualmente.
    // CONV-04: o texto digitado no input vira caption (imagem/video/documento).
    async function sendMediaFile(file) {
        if (!activeConversation || !file) return;
        if (windowClosed()) { applyWindowState(activeConversation); return; }
        const input = document.getElementById('msgInput');
        const caption = (input.value || '').trim();
        const fd = new FormData();
        fd.append('file', file, file.name);
        fd.append('caption', caption);
        input.value = '';
        input.style.height = 'auto';
        showToast('Enviando mídia...');
        const resp = await fetch(`/api/conversations/${activeConversation.id}/messages/media`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${Auth.getToken()}` },
            body: fd,
        });
        if (resp.ok) {
            showToast('Mídia enviada');
        } else {
            const d = await readErrorDetail(resp);
            if (resp.status === 409 && d.code === 'WINDOW_CLOSED') {
                await handleWindowClosed(d);
                loadChat(activeConversation.id);
                return;
            }
            showToast(d.message || 'Falha ao enviar a mídia.');
        }
        loadChat(activeConversation.id);
        loadConversations();
    }

    // CONV-03: player de audio embutido via blob autenticado
    window._playAudio = async function (assetId, btn) {
        const id = Number(assetId);
        if (btn) { btn.disabled = true; btn.textContent = 'Carregando...'; }
        try {
            let resp = await Auth.apiRequest(`/api/media/${id}`);
            if (resp && resp.status === 409) {
                const f = await Auth.apiRequest(`/api/media/${id}/fetch`, { method: 'POST' });
                if (!f || !f.ok) {
                    let detail = 'Falha ao baixar o áudio.';
                    try { const e = await f.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
                    showToast(detail);
                    return;
                }
                resp = await Auth.apiRequest(`/api/media/${id}`);
            }
            if (!resp || !resp.ok) { showToast('Áudio indisponível.'); return; }
            const blob = await resp.blob();
            const audio = document.createElement('audio');
            audio.controls = true;
            audio.src = URL.createObjectURL(blob);
            audio.style.maxWidth = '220px';
            if (btn && btn.parentNode) btn.parentNode.replaceChild(audio, btn);
            audio.play().catch(() => { /* autoplay bloqueado: usuario aperta play */ });
        } finally {
            if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = '&#9654; Ouvir'; }
        }
    };

    // CONV-02: abre a midia de um asset via fetch autenticado (baixa da Meta se preciso)
    window._openMedia = async function (assetId, btn) {
        const id = Number(assetId);
        if (btn) { btn.disabled = true; btn.textContent = 'Carregando...'; }
        try {
            let resp = await Auth.apiRequest(`/api/media/${id}`);
            if (resp && resp.status === 409) {
                // ainda nao espelhada — pede o download da Meta
                const f = await Auth.apiRequest(`/api/media/${id}/fetch`, { method: 'POST' });
                if (!f || !f.ok) {
                    let detail = 'Falha ao baixar a mídia.';
                    try { const e = await f.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
                    showToast(detail);
                    return;
                }
                resp = await Auth.apiRequest(`/api/media/${id}`);
            }
            if (!resp || !resp.ok) { showToast('Mídia indisponível.'); return; }
            const blob = await resp.blob();
            window.open(URL.createObjectURL(blob), '_blank');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '&#128206; Ver mídia'; }
        }
    };

    // CONV-08b: reenvio manual de mensagem outbound com falha
    window._retryMessage = async function (msgId) {
        if (!activeConversation) return;
        // CONV-WINDOW-01: reenvio e free-form — o botao some com a janela fechada,
        // mas o clique tambem e barrado aqui (e no backend, que e a autoridade).
        if (windowClosed()) { applyWindowState(activeConversation); return; }
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/messages/${Number(msgId)}/retry`,
            { method: 'POST' }
        );
        if (resp && resp.ok) {
            showToast('Mensagem reenviada');
        } else {
            const d = resp ? await readErrorDetail(resp) : {};
            if (resp && resp.status === 409 && d.code === 'WINDOW_CLOSED') {
                await handleWindowClosed(d);
                loadChat(activeConversation.id);
                return;
            }
            showToast(d.message || 'Falha ao reenviar a mensagem.');
        }
        loadChat(activeConversation.id);
        loadConversations();
    };

    // ─── Rendering ──────────────────────────────
    /**
     * CONV-WINDOW-01: cadeado vermelho da lista. Significa UMA coisa e so uma —
     * "a Meta ainda permite envio livre para este contato?". Nao e unread (badge
     * numerico), nao e online (ponto verde), nao e BIA (rotulo de responsavel),
     * nao e tag (chip colorido) e nao e fila. Vermelho e exclusivo desta regra.
     * Funciona nas cinco inboxes porque todas serializam ConversationResponse.
     */
    function windowLockHtml(conv) {
        if (conv.service_window_open !== false) return '';
        return '<span class="conv-window-lock" title="Janela de 24h encerrada" aria-label="Janela de 24h encerrada">'
            + '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" aria-hidden="true">'
            + '<path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zM9 6c0-1.66 1.34-3 3-3s3 1.34 3 3v2H9V6zm9 14H6V10h12v10z"/>'
            + '</svg></span>';
    }

    function renderConversationList() {
        const list = document.getElementById('convList');
        // PACOTE-B: categoria, busca, responsavel e tag ja vieram filtrados do
        // SQL. A ordem e a do servidor (a Fila depende disso: FIFO por
        // queued_at). NENHUM filtro de categoria em JS aqui.
        const filtered = conversations;

        // ERRO != VAZIO: falha de rede nao pode se passar por "nada aqui".
        if (listError) {
            list.innerHTML = `
                <div class="conv-list-error">
                    <p>Não foi possível carregar as conversas.</p>
                    <p style="font-size:12px; opacity:.8;">Verifique a conexão — nova tentativa no próximo ciclo.</p>
                </div>
            `;
            return;
        }

        if (filtered.length === 0) {
            const msg = INBOX_EMPTY[activeInbox] || 'Nenhuma conversa encontrada';
            list.innerHTML = `
                <div style="text-align:center; padding:40px 20px; color:var(--dark-400);">
                    <svg viewBox="0 0 24 24" width="40" height="40" fill="currentColor" opacity="0.3" style="margin-bottom:8px;"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>
                    <p style="font-size:13px;">${escapeHtml(msg)}</p>
                </div>
            `;
            return;
        }

        list.innerHTML = filtered.map(conv => {
            const initials = getInitials(conv.nome || conv.whatsapp);
            const time = formatTime(conv.updated_at);
            const isActive = activeConversation && activeConversation.id === conv.id;
            const isUnread = conv.unread_count > 0;
            const preview = conv.ultimo_msg || 'Sem mensagens';
            const respLabel = conv.responsavel_nome || 'Agente IA';

            return `
                <div class="conv-item ${isActive ? 'active' : ''} ${isUnread ? 'unread' : ''}"
                     data-id="${conv.id}" onclick="window._openConv(${conv.id})">
                    <div class="conv-avatar">
                        ${initials}
                        ${isOpenStatus(conv.status) ? '<div class="online-dot"></div>' : ''}
                    </div>
                    <div class="conv-info">
                        <div class="conv-info-top">
                            <span class="conv-name">${escapeHtml(conv.nome || conv.whatsapp)}</span>
                            ${windowLockHtml(conv)}
                            <span class="conv-time">${time}</span>
                        </div>
                        <div class="conv-preview">
                            ${escapeHtml(preview)}
                        </div>
                        <div style="font-size:10px; color:var(--dark-400); margin-top:2px; display:flex; align-items:center; gap:4px;">
                            <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                            ${escapeHtml(respLabel)}
                        </div>
                        ${(conv.tags && conv.tags.length) ? `<div style="display:flex; flex-wrap:wrap; gap:4px; margin-top:3px;">${conv.tags.map(t => tagChipHtml(t, false)).join('')}</div>` : ''}
                    </div>
                    ${isUnread ? `<div class="conv-unread-badge">${conv.unread_count}</div>` : ''}
                </div>
            `;
        }).join('');

        // PACOTE-B: nada de cap silencioso. Se o total do servidor excede o
        // que ja foi carregado, o restante fica acessivel explicitamente.
        if (listTotal > filtered.length) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'conv-list-more';
            btn.textContent = `Carregar mais (${filtered.length} de ${listTotal})`;
            btn.addEventListener('click', () => loadConversations('append'));
            list.appendChild(btn);
        }
    }

    function renderChat() {
        if (!activeConversation) return;

        const conv = activeConversation;

        // Header
        document.getElementById('chatAvatar').textContent = getInitials(conv.nome);
        document.getElementById('chatName').textContent = conv.nome || conv.whatsapp;

        // CONV-06: estado DERIVADO (status + atendente_id) — 'aguardando' nao e persistido
        const statusText = conv.status === 'encerrada' ? 'Encerrada' :
            (conv.atendente_id ? 'Em atendimento' : 'Aguardando atendimento');
        document.getElementById('chatStatus').textContent = statusText;

        // Close button label
        document.getElementById('btnCloseConv').title =
            conv.status === 'encerrada' ? 'Reabrir conversa' : 'Encerrar conversa';

        // CONV-06: botao Assumir/Liberar
        updateClaimButton(conv);

        // CONV-WINDOW-01: composer normal x bloco "Janela de 24h encerrada"
        applyWindowState(conv);

        // Messages
        const container = document.getElementById('chatMessages');
        container.innerHTML = '';

        const messages = conv.messages || [];
        let lastDate = '';

        messages.forEach(msg => {
            const msgDate = new Date(msg.created_at).toLocaleDateString('pt-BR');

            if (msgDate !== lastDate) {
                const divider = document.createElement('div');
                divider.className = 'date-divider';
                divider.innerHTML = `<span>${msgDate}</span>`;
                container.appendChild(divider);
                lastDate = msgDate;
            }

            appendMessageElement(container, msg);
        });

        container.scrollTop = container.scrollHeight;
    }

    function appendMessage(msg) {
        const container = document.getElementById('chatMessages');
        appendMessageElement(container, msg);
        container.scrollTop = container.scrollHeight;
    }

    function appendMessageElement(container, msg) {
        const bubble = document.createElement('div');
        bubble.className = `message-bubble ${msg.direction}`;

        const time = new Date(msg.created_at).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        let statusIcon = '';

        if (msg.direction === 'outbound') {
            if (msg.status === 'sending') statusIcon = '<span class="message-status">...</span>';
            else if (msg.status === 'sent') statusIcon = '<span class="message-status">&#10003;</span>';
            else if (msg.status === 'delivered') statusIcon = '<span class="message-status delivered">&#10003;&#10003;</span>';
            else if (msg.status === 'read') statusIcon = '<span class="message-status read">&#10003;&#10003;</span>';
            else if (msg.status === 'failed') {
                // CONV-WINDOW-01: `last_error` ja e um resumo SEGURO produzido por
                // whatsapp._error_result (nunca token/header/payload). Antes so
                // aparecia um X mudo e o operador nao descobria a causa real.
                const why = msg.last_error ? `Falha no envio: ${msg.last_error}` : 'Falha no envio';
                statusIcon = `<span class="message-status" style="color:var(--error)" title="${escapeHtml(why)}">&#10007;</span>`;
                // CONV-08b: reenvio manual — so para mensagens persistidas (com id do banco)
                // CONV-WINDOW-01: com a janela fechada o reenvio nao e oferecido
                // (o backend recusaria de todo jeito — 409 WINDOW_CLOSED).
                if (msg.id && !windowClosed()) {
                    statusIcon += `<button class="msg-retry-btn" onclick="window._retryMessage(${Number(msg.id)})" title="Reenviar mensagem" style="background:none; border:none; cursor:pointer; color:var(--error); font-size:13px; padding:0 2px; vertical-align:middle;">&#8635;</button>`;
                }
            }
        }

        let content = escapeHtml(msg.content);

        // CONV-04: caption real (esconde placeholders [IMAGE]/[VIDEO]/[DOCUMENT]/[AUDIO])
        const captionHtml = (msg.content && !/^\[[A-Z]+\]$/.test(msg.content)) ? `<br>${content}` : '';

        if (msg.msg_type === 'image' && msg.media_asset && msg.media_asset.id) {
            // CONV-04: imagem inline sob demanda via blob autenticado
            content = `<button class="media-inline-btn" onclick="window._showInlineMedia(${Number(msg.media_asset.id)}, 'image', this)" style="background:var(--dark-100); border:1px solid var(--dark-300); border-radius:8px; cursor:pointer; font-size:12px; padding:18px 26px; color:var(--dark-600);">&#128247; Ver imagem</button>${captionHtml}`;
        } else if (msg.msg_type === 'image' && msg.media_url) {
            content = `<img src="${escapeHtml(msg.media_url)}" style="max-width:100%; border-radius:8px; margin-bottom:4px;"><br>${content}`;
        } else if (msg.msg_type === 'template') {
            content = `<div style="font-size:10px; color:var(--primary); font-weight:600; margin-bottom:4px; display:flex; align-items:center; gap:4px;"><svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>Template</div>${content}`;
        } else if (msg.msg_type === 'audio') {
            // CONV-03: player sob demanda quando ha asset persistido
            if (msg.media_asset && msg.media_asset.id) {
                content = `<button class="audio-play-btn" onclick="window._playAudio(${Number(msg.media_asset.id)}, this)" style="background:var(--dark-100); border:1px solid var(--dark-300); border-radius:16px; cursor:pointer; font-size:12px; padding:6px 14px; color:var(--dark-600);">&#9654; Ouvir</button>` + (msg.content && msg.content !== '[AUDIO]' ? `<br>${content}` : '');
            } else {
                content = '<em>Audio</em>';
            }
        } else if (msg.msg_type === 'document') {
            // CONV-04: filename SEMPRE escapado (texto e data-attribute)
            if (msg.media_asset && msg.media_asset.id) {
                const fn = escapeHtml(msg.media_asset.filename || 'documento');
                content = `<div style="display:flex; align-items:center; gap:8px;">&#128196; <span style="font-size:12px;">${fn}</span> <button class="media-download-btn" data-fn="${fn}" onclick="window._downloadMedia(${Number(msg.media_asset.id)}, this)" style="background:none; border:1px solid var(--dark-300); border-radius:6px; cursor:pointer; font-size:11px; padding:2px 8px; color:var(--dark-500);">Baixar</button></div>${captionHtml}`;
            } else {
                content = '<em>Documento</em>';
            }
        } else if (msg.msg_type === 'video') {
            if (msg.media_asset && msg.media_asset.id) {
                content = `<button class="media-inline-btn" onclick="window._showInlineMedia(${Number(msg.media_asset.id)}, 'video', this)" style="background:var(--dark-100); border:1px solid var(--dark-300); border-radius:8px; cursor:pointer; font-size:12px; padding:18px 26px; color:var(--dark-600);">&#127909; Ver v&iacute;deo</button>${captionHtml}`;
            } else {
                content = '<em>Video</em>';
            }
        }

        // CONV-02: preview generico so para tipos SEM render dedicado
        // (audio/imagem/video/documento ja tem controles proprios — CONV-03/04)
        if (msg.media_asset && msg.media_asset.id
            && !['image', 'video', 'document', 'audio'].includes(msg.msg_type)) {
            content += `<div style="margin-top:4px;"><button class="media-preview-btn" onclick="window._openMedia(${Number(msg.media_asset.id)}, this)" style="background:none; border:1px solid var(--dark-300); border-radius:6px; cursor:pointer; font-size:11px; padding:2px 8px; color:var(--dark-500);">&#128206; Ver m&iacute;dia</button></div>`;
        }

        // SEC-CONV-01: `content` e `media_url` ja passaram por escapeHtml; o restante
        // e template estatico gerado pela app (time/statusIcon). Seguro por construcao.
        bubble.innerHTML = `
            <div class="message-content">${content}</div>
            <div class="message-meta">
                <span>${time}</span>
                ${statusIcon}
            </div>
        `;

        container.appendChild(bubble);
    }

    function renderLeadPanel() {
        if (!activeConversation) return;
        const conv = activeConversation;

        renderConvTags();  // CONV-05
        loadNotes();       // CONV-07
        const atSel = document.getElementById('selectAtendente');
        if (atSel) atSel.value = String(conv.atendente_id || 0);  // CONV-07

        document.getElementById('leadAvatar').textContent = getInitials(conv.nome);
        document.getElementById('leadName').textContent = conv.nome || conv.whatsapp;
        document.getElementById('leadPhone').textContent = formatPhone(conv.whatsapp);

        const statusEl = document.getElementById('leadStatus');
        statusEl.textContent = conv.status.toUpperCase();
        statusEl.className = `lead-profile-status ${conv.status}`;

        document.getElementById('leadId').textContent = conv.lead_id > 0 ? `#${conv.lead_id}` : 'Nao vinculado';
        document.getElementById('leadWhatsapp').textContent = conv.whatsapp;
        document.getElementById('leadCreatedAt').textContent = new Date(conv.created_at).toLocaleDateString('pt-BR');
        document.getElementById('leadMsgCount').textContent = (conv.messages || []).length;

        // Set responsavel selector
        const respSelect = document.getElementById('selectResponsavel');
        respSelect.value = conv.responsavel_id || '0';

        // CRM link buttons
        if (conv.lead_id > 0) {
            document.getElementById('btnViewCRM').href = `${CRM_BASE_URL}/leads?open=${conv.lead_id}`;
            document.getElementById('btnViewCRM').style.display = 'flex';
            document.getElementById('btnViewPipeline').href = `${CRM_BASE_URL}/pipeline?lead_id=${conv.lead_id}`;
            document.getElementById('btnViewPipeline').style.display = 'flex';
            document.getElementById('btnLinkLead').style.display = 'none';
        } else {
            document.getElementById('btnViewCRM').style.display = 'none';
            document.getElementById('btnViewPipeline').style.display = 'none';
            document.getElementById('btnLinkLead').style.display = 'flex';
        }

        const botBtn = document.getElementById('btnToggleBotText');
        botBtn.textContent = conv.is_bot_active ? 'Desativar Bot' : 'Ativar Bot';
    }

    // ─── CONV-VAR-01: seletor de variaveis (@TOKEN) no composer ───
    // Fonte: /api/variables (somente ativas). Clicar insere o token na posicao
    // atual do cursor — NUNCA envia a mensagem. A resolucao do valor acontece
    // no BACKEND, no momento do envio; aqui so inserimos o token.
    // Itens renderizados com createElement/textContent (nome/token sao
    // controlados por administradores — nunca interpolados como HTML).
    let varPaletteOpen = false;
    let varPaletteItems = [];
    let varFetchSeq = 0;

    function insertAtCursor(field, textToInsert) {
        const start = field.selectionStart ?? field.value.length;
        const end = field.selectionEnd ?? field.value.length;
        field.value = field.value.slice(0, start) + textToInsert + field.value.slice(end);
        const caret = start + textToInsert.length;
        field.focus();
        field.setSelectionRange(caret, caret);
    }

    async function fetchVariables() {
        const seq = ++varFetchSeq;
        const resp = await Auth.apiRequest('/api/variables');
        if (!resp || !resp.ok || seq !== varFetchSeq) return;
        const data = await resp.json();
        varPaletteItems = data.variables || [];
        if (varPaletteOpen) renderVarPalette();
    }

    function toggleVarPalette() {
        if (varPaletteOpen) {
            closeVarPalette();
            return;
        }
        varPaletteOpen = true;
        document.getElementById('varPalette').style.display = 'block';
        document.getElementById('btnVars').setAttribute('aria-expanded', 'true');
        renderVarPalette();
        fetchVariables();   // recarrega a cada abertura (pega edicoes do settings)
    }

    function closeVarPalette() {
        varPaletteOpen = false;
        document.getElementById('varPalette').style.display = 'none';
        document.getElementById('btnVars').setAttribute('aria-expanded', 'false');
    }

    function renderVarPalette() {
        const palette = document.getElementById('varPalette');
        palette.replaceChildren();

        const header = document.createElement('div');
        header.className = 'var-palette-header';
        header.textContent = 'Inserir variável';
        palette.appendChild(header);

        if (varPaletteItems.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'var-palette-empty';
            empty.textContent = 'Nenhuma variável cadastrada (crie em Configurações).';
            palette.appendChild(empty);
            return;
        }

        varPaletteItems.forEach(v => {
            const item = document.createElement('div');
            item.className = 'var-palette-item';
            item.setAttribute('role', 'option');
            const token = document.createElement('div');
            token.className = 'var-palette-token';
            token.textContent = v.token || '';
            const name = document.createElement('div');
            name.className = 'var-palette-name';
            name.textContent = v.name || '';
            item.appendChild(token);
            item.appendChild(name);
            item.addEventListener('click', () => {
                insertAtCursor(document.getElementById('msgInput'), v.token);
                closeVarPalette();
            });
            palette.appendChild(item);
        });
    }

    // ─── CONV-VAR-01-HARD-01: previa da mensagem ───
    // O texto renderizado vem SEMPRE de POST /api/variables/preview, que usa o
    // MESMO `render()` do envio. O JS nao reimplementa resolucao nenhuma e
    // NUNCA envia a mensagem — o backend segue sendo a validacao final.
    const PREVIEW_PROBLEM_LABELS = {
        unknown: 'não é uma variável cadastrada',
        inactive: 'variável desativada',
        empty_fixed: 'sem valor configurado',
        empty_dynamic: 'sem valor para este contato',
        invalid_source: 'origem inválida',
        ambiguous: 'colada a outro texto',
    };

    function previewBlock(titulo, texto, extraClass) {
        const wrap = document.createElement('div');
        wrap.className = 'preview-block';
        const label = document.createElement('div');
        label.className = 'preview-label';
        label.textContent = titulo;
        const body = document.createElement('div');
        body.className = 'preview-text' + (extraClass ? ' ' + extraClass : '');
        body.textContent = texto;
        wrap.appendChild(label);
        wrap.appendChild(body);
        return wrap;
    }

    async function openPreview() {
        const input = document.getElementById('msgInput');
        const original = input.value;
        if (!original.trim() || !activeConversation) {
            showToast('Escreva a mensagem antes de visualizar.');
            return;
        }

        const resp = await Auth.apiRequest('/api/variables/preview', {
            method: 'POST',
            body: JSON.stringify({ text: original, conversation_id: activeConversation.id }),
        });
        if (!resp || !resp.ok) {
            showToast('Não foi possível gerar a prévia.');
            return;
        }
        const data = await resp.json();
        renderPreview(original, data);
    }

    function renderPreview(original, data) {
        const body = document.getElementById('previewModalBody');
        body.replaceChildren();

        const problems = data.problems || [];
        const semVariavel = data.rendered === original && problems.length === 0;

        if (semVariavel) {
            // Sem variaveis: um bloco so, texto inalterado.
            body.appendChild(previewBlock('Mensagem (sem variáveis)', original));
        } else {
            body.appendChild(previewBlock('Texto original', original, 'preview-original'));
            // Com problemas, o texto NAO sera enviado — rotula-lo como "o que
            // o cliente vai receber" seria mentira (e contradiria o aviso
            // logo abaixo).
            body.appendChild(previewBlock(
                problems.length ? 'Resultado parcial (não será enviado)' : 'Como o cliente vai receber',
                data.rendered,
                problems.length ? 'preview-original' : ''
            ));
        }

        const status = document.createElement('div');
        status.className = 'preview-status ' + (problems.length ? 'has-problems' : 'ok');
        status.textContent = problems.length
            ? 'Esta mensagem NÃO pode ser enviada até corrigir:'
            : 'Tudo certo — a mensagem pode ser enviada.';
        body.appendChild(status);

        if (problems.length) {
            const list = document.createElement('ul');
            list.className = 'preview-problems';
            problems.forEach(p => {
                const item = document.createElement('li');
                const tok = document.createElement('span');
                tok.className = 'preview-problem-token';
                tok.textContent = p.token;
                const why = document.createElement('span');
                why.textContent = ' — ' + (PREVIEW_PROBLEM_LABELS[p.code] || p.short || p.code);
                item.appendChild(tok);
                item.appendChild(why);
                list.appendChild(item);
            });
            body.appendChild(list);
        }

        document.getElementById('previewModalOverlay').style.display = 'flex';
    }

    function closePreview() {
        document.getElementById('previewModalOverlay').style.display = 'none';
    }

    // ─── Helpers ─────────────────────────────────
    function getInitials(name) {
        if (!name) return '?';
        return name.split(' ').map(n => n[0]).slice(0, 2).join('').toUpperCase();
    }

    function formatTime(dateStr) {
        if (!dateStr) return '';
        const date = new Date(dateStr);
        const now = new Date();
        const diff = now - date;

        if (diff < 86400000 && date.getDate() === now.getDate()) {
            return date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        }

        if (diff < 172800000) return 'Ontem';

        return date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
    }

    function formatPhone(phone) {
        if (!phone) return '';
        const clean = phone.replace(/\D/g, '');
        if (clean.length === 13) {
            return `+${clean.slice(0, 2)} (${clean.slice(2, 4)}) ${clean.slice(4, 9)}-${clean.slice(9)}`;
        }
        return phone;
    }

    function escapeHtml(text) {
        // SEC-CONV-01: escapa tambem aspas simples/duplas (protege contexto de atributo).
        // Toda interpolacao de dado nao-confiavel em innerHTML DEVE passar por aqui.
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function showToast(message) {
        const existing = document.querySelector('.toast');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.textContent = message;
        document.body.appendChild(toast);

        setTimeout(() => toast.remove(), 3500);
    }

    // ─── Global handlers ────────────────────────
    window._openConv = function (id) {
        loadChat(id);
    };

})();
