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
    let searchTerm = '';
    let pollInterval = null;
    let usersCache = [];

    // CRM base URL — will be derived from window location
    const CRM_BASE_URL = window.location.port === '8001'
        ? window.location.protocol + '//' + window.location.hostname + ':8000'
        : window.location.origin.replace(':8001', ':8000');

    // ─── Init ───────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        if (!Auth.requireAuth()) return;

        loadUsers();
        setupEventListeners();
        loadConversations();

        // Poll for new messages every 5 seconds
        pollInterval = setInterval(loadConversations, 5000);
    });

    // ─── Load Users ─────────────────────────────
    async function loadUsers() {
        const resp = await Auth.apiRequest('/api/conversations/users');
        if (!resp || !resp.ok) return;

        const data = await resp.json();
        usersCache = data.users || [];

        // Populate filter dropdown
        const filterSelect = document.getElementById('filterResponsavel');
        // Keep first two options (Todos / Agente IA)
        usersCache.forEach(u => {
            const opt = document.createElement('option');
            opt.value = u.id;
            opt.textContent = u.nome;
            filterSelect.appendChild(opt);
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
        document.querySelectorAll('.conv-filters button').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.conv-filters button').forEach(b => b.classList.remove('active'));
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

        // Send message
        document.getElementById('btnSend').addEventListener('click', sendMessage);

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

        // Check URL params — open specific lead conversation
        const params = new URLSearchParams(window.location.search);
        const leadId = params.get('lead_id');
        if (leadId) {
            loadConversationByLead(parseInt(leadId));
        }
    }

    // ─── API Calls ──────────────────────────────
    async function loadConversations() {
        let url = '/api/conversations?limit=200';
        if (activeResponsavelFilter !== '') {
            url += `&responsavel_id=${activeResponsavelFilter}`;
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
            }
        } catch (e) {
            console.log('No conversation found for lead', leadId);
        }
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
        }
    }

    // ─── Rendering ──────────────────────────────
    function renderConversationList() {
        const list = document.getElementById('convList');
        let filtered = conversations;

        if (activeFilter !== 'all') {
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

        const statusText = conv.status === 'aberta' ? 'Online' :
            conv.status === 'aguardando' ? 'Aguardando' : 'Encerrada';
        document.getElementById('chatStatus').textContent = statusText;

        // Close button label
        document.getElementById('btnCloseConv').title =
            conv.status === 'encerrada' ? 'Reabrir conversa' : 'Encerrar conversa';

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
            else if (msg.status === 'failed') statusIcon = '<span class="message-status" style="color:var(--error)">&#10007;</span>';
        }

        let content = escapeHtml(msg.content);

        if (msg.msg_type === 'image' && msg.media_url) {
            content = `<img src="${msg.media_url}" style="max-width:100%; border-radius:8px; margin-bottom:4px;"><br>${content}`;
        } else if (msg.msg_type === 'audio') {
            content = '<em>Audio</em>';
        } else if (msg.msg_type === 'document') {
            content = '<em>Documento</em>';
        } else if (msg.msg_type === 'video') {
            content = '<em>Video</em>';
        }

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
            document.getElementById('btnViewCRM').href = `${CRM_BASE_URL}/leads?search=${encodeURIComponent(conv.whatsapp)}`;
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
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
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
