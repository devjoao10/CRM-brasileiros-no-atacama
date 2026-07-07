/**
 * Conversas.js — Main application logic
 * Handles conversation list, chat, lead info panel, and CRM integration.
 */

(function () {
    'use strict';

    // ─── State ──────────────────────────────────
    let conversations = [];
    let activeConversation = null;
    let activeFilter = 'all';
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

    // ─── Init ───────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        if (!Auth.requireAuth()) return;

        loadUsers();
        setupEventListeners();
        loadTags();          // CONV-05
        loadConversations();

        // Poll for new messages every 5 seconds
        pollInterval = setInterval(async () => {
            loadConversations();
            if (activeConversation) {
                const resp = await Auth.apiRequest(`/api/conversations/${activeConversation.id}`);
                if (!resp || !resp.ok) return;
                const data = await resp.json();
                const oldCount = (activeConversation.messages || []).length;
                const newCount = (data.messages || []).length;
                if (newCount !== oldCount) {
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
        document.getElementById('searchInput').addEventListener('input', (e) => {
            searchTerm = e.target.value.toLowerCase();
            renderConversationList();
        });

        // Status filters
        document.querySelectorAll('.conv-filter-tabs button').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.conv-filter-tabs button').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                activeFilter = btn.dataset.filter;
                renderConversationList();
            });
        });

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
        document.getElementById('btnNewTag').addEventListener('click', async () => {
            const nome = (prompt('Nome da nova tag:') || '').trim();
            if (!nome) return;
            const resp = await Auth.apiRequest('/api/tags', {
                method: 'POST',
                body: JSON.stringify({ nome: nome, cor: '#3B82F6' }),
            });
            if (resp && resp.ok) {
                showToast('Tag criada');
                await loadTags();
            } else {
                let detail = 'Falha ao criar a tag.';
                try { const err = await resp.json(); if (err && err.detail) detail = err.detail; } catch (_) { }
                showToast(detail);
            }
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
        const btnShowTemplates = document.getElementById('btnShowTemplates');
        const btnCloseTemplates = document.getElementById('btnCloseTemplates');
        const templatesDropdown = document.getElementById('templatesDropdown');
        const templatesList = document.getElementById('templatesList');

        if (btnShowTemplates) {
            btnShowTemplates.addEventListener('click', async () => {
                templatesDropdown.style.display = 'block';
                templatesList.innerHTML = '<div style="padding: 10px; text-align: center; font-size: 12px; color: var(--dark-400);">Carregando...</div>';
                
                try {
                    // Fetch ALL templates — not filtered by status
                    const res = await Auth.apiRequest('/api/templates');
                    if (!res.ok) throw new Error();
                    const data = await res.json();
                    
                    if (!data.templates || data.templates.length === 0) {
                        templatesList.innerHTML = '<div style="padding: 10px; text-align: center; font-size: 12px; color: var(--dark-400);">Nenhum template cadastrado.<br><a href="/templates" style="color:var(--primary);">Criar template</a></div>';
                        return;
                    }
                    
                    templatesList.innerHTML = '';
                    data.templates.forEach(t => {
                        const el = document.createElement('div');
                        el.style.cssText = 'padding: 10px; border-radius: 6px; cursor: pointer; transition: background 0.2s; margin-bottom: 4px;';
                        el.onmouseover = () => el.style.background = 'var(--dark-100)';
                        el.onmouseout = () => el.style.background = 'transparent';
                        
                        // Name row with status badge
                        const nameRow = document.createElement('div');
                        nameRow.style.cssText = 'display: flex; align-items: center; gap: 6px; margin-bottom: 2px;';
                        
                        const name = document.createElement('span');
                        name.style.cssText = 'font-weight: 600; font-size: 12px; color: var(--primary);';
                        name.textContent = t.name;
                        
                        const badge = document.createElement('span');
                        const badgeColors = {
                            'APPROVED': { bg: '#e6f4ea', color: '#1e7e34' },
                            'PENDING': { bg: '#fff8e1', color: '#f57f17' },
                            'REJECTED': { bg: '#fde8e8', color: '#c62828' },
                        };
                        const bc = badgeColors[t.status] || { bg: '#eee', color: '#666' };
                        badge.style.cssText = `font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 4px; background: ${bc.bg}; color: ${bc.color}; text-transform: uppercase;`;
                        badge.textContent = t.status === 'APPROVED' ? 'Aprovado' : t.status === 'PENDING' ? 'Em Análise' : t.status;
                        
                        nameRow.appendChild(name);
                        nameRow.appendChild(badge);
                        
                        const body = document.createElement('div');
                        body.style.cssText = 'font-size: 11px; color: var(--dark-500); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;';
                        body.textContent = t.body_text;
                        
                        el.appendChild(nameRow);
                        el.appendChild(body);
                        
                        el.addEventListener('click', async () => {
                            templatesDropdown.style.display = 'none';
                            if (!activeConversation) return;

                            try {
                                let sendRes;
                                if (t.status === 'APPROVED') {
                                    sendRes = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/messages`, {
                                        method: 'POST',
                                        body: JSON.stringify({
                                            content: t.body_text,
                                            msg_type: 'template',
                                            template_name: t.name
                                        })
                                    });
                                } else {
                                    sendRes = await Auth.apiRequest(`/api/conversations/${activeConversation.id}/messages`, {
                                        method: 'POST',
                                        body: JSON.stringify({
                                            content: t.body_text,
                                            msg_type: 'text'
                                        })
                                    });
                                }

                                if (sendRes.ok) {
                                    showToast(t.status === 'APPROVED' ? 'Template oficial enviado!' : 'Mensagem enviada como texto!');
                                    loadChat(activeConversation.id);
                                    loadConversations();
                                } else {
                                    const err = await sendRes.json();
                                    showToast(err.detail || 'Erro ao enviar');
                                }
                            } catch (e) {
                                showToast('Erro de conexão');
                            }
                        });
                        
                        templatesList.appendChild(el);
                    });
                } catch (e) {
                    templatesList.innerHTML = '<div style="padding: 10px; text-align: center; font-size: 12px; color: var(--error);">Erro ao carregar templates.</div>';
                }
            });
            
            btnCloseTemplates.addEventListener('click', () => {
                templatesDropdown.style.display = 'none';
            });
            
            // Close dropdown when clicking outside
            document.addEventListener('click', (e) => {
                if (!templatesDropdown.contains(e.target) && e.target !== btnShowTemplates && !btnShowTemplates.contains(e.target)) {
                    templatesDropdown.style.display = 'none';
                }
            });
        }
        // --- END TEMPLATE LOGIC ---

        // Send message
        document.getElementById('btnSend').addEventListener('click', sendMessage);

        // CONV-BF-UI-03: drag-to-scroll das abas de filtro — ESCOPADO ao
        // container das abas (nao sequestra eventos globais; arrasto real
        // nao dispara troca de aba; roda do mouse rola horizontalmente).
        (function initTabsDragScroll() {
            const firstTab = document.querySelector('.conv-filter-tabs button[data-filter]');
            const bar = firstTab ? firstTab.parentElement : null;
            if (!bar) return;
            let isDown = false, startX = 0, startScroll = 0, dragged = false;
            bar.addEventListener('mousedown', (e) => {
                isDown = true;
                dragged = false;
                startX = e.pageX;
                startScroll = bar.scrollLeft;
            });
            window.addEventListener('mousemove', (e) => {
                if (!isDown) return;
                const dx = e.pageX - startX;
                if (Math.abs(dx) > 5) dragged = true;
                bar.scrollLeft = startScroll - dx;
            });
            window.addEventListener('mouseup', () => { isDown = false; });
            // clique que na verdade foi arrasto nao muda o filtro
            bar.addEventListener('click', (e) => {
                if (dragged) {
                    e.stopPropagation();
                    e.preventDefault();
                    dragged = false;
                }
            }, true);
            bar.addEventListener('wheel', (e) => {
                if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
                    bar.scrollLeft += e.deltaY;
                    e.preventDefault();
                }
            }, { passive: false });
        })();

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

        // Textarea: Enter to send, Shift+Enter for newline
        document.getElementById('msgInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
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
    async function loadConversations() {
        let url = '/api/conversations?limit=200';
        if (activeResponsavelFilter !== '') {
            url += `&responsavel_id=${activeResponsavelFilter}`;
        }
        if (activeTagFilter !== '') {
            url += `&tag_id=${Number(activeTagFilter)}`;  // CONV-05
        }

        const resp = await Auth.apiRequest(url);
        if (!resp || !resp.ok) return;

        const data = await resp.json();
        conversations = data.conversations || [];
        renderConversationList();
    }

    async function loadChat(conversationId) {
        const resp = await Auth.apiRequest(`/api/conversations/${conversationId}`);
        if (!resp || !resp.ok) return;

        activeConversation = await resp.json();
        renderChat();
        renderLeadPanel();

        // Show chat, hide empty state
        document.getElementById('chatEmpty').style.display = 'none';
        const chatActive = document.getElementById('chatActive');
        chatActive.style.display = 'flex';
        chatActive.style.flexDirection = 'column';
        document.getElementById('leadPanel').style.display = 'flex';

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

    async function sendMessage() {
        const input = document.getElementById('msgInput');
        const content = input.value.trim();
        if (!content || !activeConversation) return;

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
            let detail = 'Falha ao enviar a mensagem. Tente novamente.';
            try {
                if (resp) {
                    const err = await resp.json();
                    if (err && err.detail) detail = err.detail;
                }
            } catch (_) { /* resposta sem corpo JSON */ }
            showToast(detail);
            loadChat(activeConversation.id);
        }
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

    // ─── CONV-06: Fila (assumir/liberar) ─────────
    function updateClaimButton(conv) {
        const btn = document.getElementById('btnClaim');
        if (!btn) return;
        const me = Auth.getUser() || {};
        if (conv.status !== 'aberta') {
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
            let detail = 'Falha ao enviar a mídia.';
            try { const e = await resp.json(); if (e && e.detail) detail = e.detail; } catch (_) { }
            showToast(detail);
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
        const resp = await Auth.apiRequest(
            `/api/conversations/${activeConversation.id}/messages/${Number(msgId)}/retry`,
            { method: 'POST' }
        );
        if (resp && resp.ok) {
            showToast('Mensagem reenviada');
        } else {
            let detail = 'Falha ao reenviar a mensagem.';
            try {
                if (resp) {
                    const err = await resp.json();
                    if (err && err.detail) detail = err.detail;
                }
            } catch (_) { /* resposta sem corpo JSON */ }
            showToast(detail);
        }
        loadChat(activeConversation.id);
        loadConversations();
    };

    // ─── Rendering ──────────────────────────────
    function renderConversationList() {
        const list = document.getElementById('convList');
        let filtered = conversations;

        if (activeFilter === 'aguardando') {
            // CONV-06: fila = derivado (aberta + sem atendente)
            filtered = filtered.filter(c => c.status === 'aberta' && !c.atendente_id);
        } else if (activeFilter === 'minhas') {
            // CONV-07: atribuidas a mim
            const me = Auth.getUser() || {};
            filtered = filtered.filter(c => c.atendente_id === me.id);
        } else if (activeFilter !== 'all') {
            filtered = filtered.filter(c => c.status === activeFilter);
        }

        if (searchTerm) {
            filtered = filtered.filter(c =>
                (c.nome || '').toLowerCase().includes(searchTerm) ||
                (c.whatsapp || '').includes(searchTerm)
            );
        }

        // Update badge
        const openCount = conversations.filter(c => c.status === 'aberta' && c.unread_count > 0).length;
        const badge = document.getElementById('badgeAberta');
        if (openCount > 0) {
            badge.textContent = openCount;
            badge.style.display = 'inline-block';
        } else {
            badge.style.display = 'none';
        }

        if (filtered.length === 0) {
            list.innerHTML = `
                <div style="text-align:center; padding:40px 20px; color:var(--dark-400);">
                    <svg viewBox="0 0 24 24" width="40" height="40" fill="currentColor" opacity="0.3" style="margin-bottom:8px;"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>
                    <p style="font-size:13px;">Nenhuma conversa encontrada</p>
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
                        ${conv.status === 'aberta' ? '<div class="online-dot"></div>' : ''}
                    </div>
                    <div class="conv-info">
                        <div class="conv-info-top">
                            <span class="conv-name">${escapeHtml(conv.nome || conv.whatsapp)}</span>
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
                statusIcon = '<span class="message-status" style="color:var(--error)" title="Falha no envio">&#10007;</span>';
                // CONV-08b: reenvio manual — so para mensagens persistidas (com id do banco)
                if (msg.id) {
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
