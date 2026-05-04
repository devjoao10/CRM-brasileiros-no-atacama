/**
 * Settings.js — Settings page logic.
 * Handles API config, auto-replies, business hours, and quick replies.
 */
(function () {
    'use strict';

    const WEEKDAYS = ['Segunda', 'Terca', 'Quarta', 'Quinta', 'Sexta', 'Sabado', 'Domingo'];
    let quickReplies = [];
    let editingQRId = null;

    document.addEventListener('DOMContentLoaded', () => {
        if (!Auth.requireAuth()) return;
        setupEventListeners();
        loadApiConfig();
        loadAutoReplies();
        loadBusinessHours();
        loadQuickReplies();
    });

    function setupEventListeners() {
        document.getElementById('btnLogout').addEventListener('click', () => Auth.logout());

        // Tabs
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(`panel-${tab.dataset.tab}`).classList.add('active');
            });
        });

        // API Config
        document.getElementById('btnSaveApiConfig').addEventListener('click', saveApiConfig);
        document.getElementById('btnTestConnection').addEventListener('click', testApiConnection);
        document.getElementById('btnConnectApi').addEventListener('click', connectApi);
        document.getElementById('btnDisconnectApi').addEventListener('click', disconnectApi);
        document.getElementById('btnToggleToken').addEventListener('click', () => {
            const input = document.getElementById('cfgAccessToken');
            input.type = input.type === 'password' ? 'text' : 'password';
        });

        // Set webhook URL reference
        const webhookUrl = `${window.location.origin}/webhook`;
        document.getElementById('cfgWebhookUrl').value = webhookUrl;

        // Business hours save
        document.getElementById('btnSaveHours').addEventListener('click', saveBusinessHours);

        // Quick replies modal
        document.getElementById('btnNewQuickReply').addEventListener('click', () => openQRModal());
        document.getElementById('qrModalClose').addEventListener('click', () => closeQRModal());
        document.getElementById('btnCancelQR').addEventListener('click', () => closeQRModal());
        document.getElementById('qrModalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeQRModal();
        });
        document.getElementById('qrForm').addEventListener('submit', handleSaveQR);
    }

    // ─── API Configuration ──────────────────────────
    async function loadApiConfig() {
        const resp = await Auth.apiRequest('/api/config');
        if (!resp || !resp.ok) {
            updateApiStatusBanner(false, 'Erro ao carregar configuracao');
            return;
        }
        const config = await resp.json();
        renderApiConfig(config);
    }

    function renderApiConfig(config) {
        // Don't overwrite if user is currently editing
        if (!document.getElementById('cfgAccessToken').value) {
            document.getElementById('cfgAccessToken').value = '';
            document.getElementById('cfgAccessToken').placeholder = config.has_access_token
                ? '(Token salvo — insira para alterar)'
                : 'EAAxxxxxxx...';
        }
        document.getElementById('cfgWabaId').value = config.meta_waba_id || '';
        document.getElementById('cfgPhoneId').value = config.meta_phone_number_id || '';
        document.getElementById('cfgVerifyToken').value = config.meta_verify_token || '';
        document.getElementById('cfgApiVersion').value = config.meta_api_version || 'v21.0';
        if (config.webhook_url) {
            document.getElementById('cfgWebhookUrl').value = config.webhook_url;
        }

        updateApiStatusBanner(config.is_connected);
        updateConnectionButtons(config.is_connected, config.has_access_token);
    }

    function updateApiStatusBanner(connected, errorText = null) {
        const dot = document.getElementById('apiStatusDot');
        const text = document.getElementById('apiStatusText');
        const details = document.getElementById('apiStatusDetails');

        dot.className = 'api-status-dot ' + (connected ? 'connected' : 'disconnected');
        text.textContent = connected ? 'Conectado a API WhatsApp' : 'Desconectado';
        if (errorText) {
            text.textContent = errorText;
        }
        details.textContent = connected
            ? 'Mensagens e templates funcionando via API oficial'
            : 'Configure as credenciais abaixo para ativar';
    }

    function updateConnectionButtons(connected, hasToken) {
        const connectBtn = document.getElementById('btnConnectApi');
        const disconnectBtn = document.getElementById('btnDisconnectApi');

        if (connected) {
            connectBtn.style.display = 'none';
            disconnectBtn.style.display = 'inline-flex';
        } else if (hasToken) {
            connectBtn.style.display = 'inline-flex';
            disconnectBtn.style.display = 'none';
        } else {
            connectBtn.style.display = 'none';
            disconnectBtn.style.display = 'none';
        }
    }

    async function saveApiConfig() {
        const payload = {};
        const token = document.getElementById('cfgAccessToken').value.trim();
        if (token) payload.meta_access_token = token;

        const wabaId = document.getElementById('cfgWabaId').value.trim();
        if (wabaId !== undefined) payload.meta_waba_id = wabaId || null;

        const phoneId = document.getElementById('cfgPhoneId').value.trim();
        if (phoneId !== undefined) payload.meta_phone_number_id = phoneId || null;

        const verifyToken = document.getElementById('cfgVerifyToken').value.trim();
        if (verifyToken !== undefined) payload.meta_verify_token = verifyToken || null;

        const apiVersion = document.getElementById('cfgApiVersion').value.trim();
        if (apiVersion) payload.meta_api_version = apiVersion;

        const webhookUrl = document.getElementById('cfgWebhookUrl').value.trim();
        if (webhookUrl) payload.webhook_url = webhookUrl;

        const resp = await Auth.apiRequest('/api/config', {
            method: 'PUT',
            body: JSON.stringify(payload),
        });

        if (resp && resp.ok) {
            showToast('Credenciais salvas');
            const config = await resp.json();
            renderApiConfig(config);
            // Clear password field after save
            document.getElementById('cfgAccessToken').value = '';
        } else {
            const err = await resp.json().catch(() => null);
            showToast(err?.detail || 'Erro ao salvar credenciais');
        }
    }

    async function testApiConnection() {
        const btn = document.getElementById('btnTestConnection');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" class="spin"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg> Testando...';
        btn.disabled = true;

        try {
            const resp = await Auth.apiRequest('/api/config/test', { method: 'POST' });
            if (!resp) return;

            const result = await resp.json();
            if (result.success) {
                let detailsHtml = '';
                if (result.waba_name) detailsHtml += `WABA: ${result.waba_name}`;
                if (result.phone_display) detailsHtml += ` | Tel: ${result.phone_display}`;
                if (result.phone_name) detailsHtml += ` | Nome: ${result.phone_name}`;
                if (result.phone_quality) detailsHtml += ` | Qualidade: ${result.phone_quality}`;

                document.getElementById('apiStatusDetails').textContent = detailsHtml;
                showToast('Conexao bem-sucedida!');
                updateConnectionButtons(false, true);
                document.getElementById('btnConnectApi').style.display = 'inline-flex';
            } else {
                showToast('Falha na conexao: ' + (result.error || 'Erro desconhecido'));
                document.getElementById('apiStatusDetails').textContent = result.error || '';
            }
        } catch (e) {
            showToast('Erro ao testar conexao');
        } finally {
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    }

    async function connectApi() {
        const btn = document.getElementById('btnConnectApi');
        btn.disabled = true;

        try {
            const resp = await Auth.apiRequest('/api/config/connect', { method: 'POST' });
            if (resp && resp.ok) {
                const result = await resp.json();
                showToast('API conectada com sucesso!');
                updateApiStatusBanner(true);
                updateConnectionButtons(true, true);

                let details = '';
                if (result.waba_name) details += `WABA: ${result.waba_name}`;
                if (result.phone_display) details += ` | Tel: ${result.phone_display}`;
                document.getElementById('apiStatusDetails').textContent = details || 'Conectado';
            } else {
                const err = await resp.json().catch(() => null);
                showToast(err?.detail || 'Falha ao conectar');
            }
        } finally {
            btn.disabled = false;
        }
    }

    async function disconnectApi() {
        if (!confirm('Desconectar a API WhatsApp? O envio de mensagens sera pausado.')) return;

        const resp = await Auth.apiRequest('/api/config/disconnect', { method: 'POST' });
        if (resp && resp.ok) {
            showToast('API desconectada');
            updateApiStatusBanner(false);
            updateConnectionButtons(false, true);
        }
    }

    // ─── Auto Replies ────────────────────────────
    async function loadAutoReplies() {
        const resp = await Auth.apiRequest('/api/settings/auto-replies');
        if (!resp || !resp.ok) return;
        const data = await resp.json();
        renderAutoReplies(data.auto_replies || []);
    }

    function renderAutoReplies(replies) {
        const list = document.getElementById('autoRepliesList');
        list.innerHTML = replies.map(r => `
            <div class="auto-reply-item">
                <div class="auto-reply-header">
                    <div class="auto-reply-info">
                        <span class="auto-reply-title">${escapeHtml(r.title)}</span>
                        <span class="auto-reply-trigger">${r.trigger}</span>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" ${r.is_active ? 'checked' : ''} onchange="window._toggleAutoReply('${r.trigger}', this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                </div>
                <textarea class="auto-reply-message" id="ar-${r.trigger}" rows="3" 
                    placeholder="Mensagem automatica...">${escapeHtml(r.message)}</textarea>
                <div class="auto-reply-actions">
                    <button class="btn-sm btn-primary" onclick="window._saveAutoReply('${r.trigger}')">Salvar</button>
                </div>
            </div>
        `).join('');
    }

    window._toggleAutoReply = async function (trigger, isActive) {
        await Auth.apiRequest(`/api/settings/auto-replies/${trigger}`, {
            method: 'PUT',
            body: JSON.stringify({ is_active: isActive }),
        });
        showToast(isActive ? 'Frase ativada' : 'Frase desativada');
    };

    window._saveAutoReply = async function (trigger) {
        const message = document.getElementById(`ar-${trigger}`).value;
        const resp = await Auth.apiRequest(`/api/settings/auto-replies/${trigger}`, {
            method: 'PUT',
            body: JSON.stringify({ message }),
        });
        if (resp && resp.ok) showToast('Frase atualizada');
        else showToast('Erro ao salvar');
    };

    // ─── Business Hours ─────────────────────────
    async function loadBusinessHours() {
        const resp = await Auth.apiRequest('/api/settings/business-hours');
        if (!resp || !resp.ok) return;
        const data = await resp.json();
        renderBusinessHours(data.hours || []);
    }

    function renderBusinessHours(hours) {
        const list = document.getElementById('businessHoursList');
        list.innerHTML = hours.map(h => `
            <div class="bh-row" data-weekday="${h.weekday}">
                <div class="bh-day">
                    <label class="toggle-switch">
                        <input type="checkbox" class="bh-open" ${h.is_open ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                    <span class="bh-day-name">${WEEKDAYS[h.weekday]}</span>
                </div>
                <div class="bh-times">
                    <input type="time" class="bh-time bh-open-time" value="${h.open_time || '09:00'}" ${!h.is_open ? 'disabled' : ''}>
                    <span class="bh-separator">ate</span>
                    <input type="time" class="bh-time bh-close-time" value="${h.close_time || '18:00'}" ${!h.is_open ? 'disabled' : ''}>
                </div>
            </div>
        `).join('');

        // Toggle time inputs on checkbox change
        list.querySelectorAll('.bh-open').forEach(cb => {
            cb.addEventListener('change', function () {
                const row = this.closest('.bh-row');
                row.querySelectorAll('.bh-time').forEach(input => {
                    input.disabled = !this.checked;
                });
            });
        });
    }

    async function saveBusinessHours() {
        const rows = document.querySelectorAll('.bh-row');
        const days = Array.from(rows).map(row => ({
            weekday: parseInt(row.dataset.weekday),
            is_open: row.querySelector('.bh-open').checked,
            open_time: row.querySelector('.bh-open-time').value || null,
            close_time: row.querySelector('.bh-close-time').value || null,
        }));

        const resp = await Auth.apiRequest('/api/settings/business-hours', {
            method: 'PUT',
            body: JSON.stringify({ days }),
        });

        if (resp && resp.ok) showToast('Horarios salvos');
        else showToast('Erro ao salvar horarios');
    }

    // ─── Quick Replies ──────────────────────────
    async function loadQuickReplies() {
        const resp = await Auth.apiRequest('/api/quick-replies?active_only=false');
        if (!resp || !resp.ok) return;
        const data = await resp.json();
        quickReplies = data.quick_replies || [];
        renderQuickReplies();
    }

    function renderQuickReplies() {
        const list = document.getElementById('quickRepliesList');
        if (quickReplies.length === 0) {
            list.innerHTML = '<div class="empty-state"><p>Nenhuma mensagem rapida cadastrada</p></div>';
            return;
        }

        list.innerHTML = quickReplies.map(qr => `
            <div class="qr-item ${!qr.is_active ? 'inactive' : ''}">
                <div class="qr-item-main">
                    <div class="qr-shortcut">${escapeHtml(qr.shortcut)}</div>
                    <div class="qr-title">${escapeHtml(qr.title)}</div>
                    <div class="qr-preview">${escapeHtml(qr.content).substring(0, 100)}${qr.content.length > 100 ? '...' : ''}</div>
                </div>
                <div class="qr-item-meta">
                    ${qr.category ? `<span class="qr-category">${escapeHtml(qr.category)}</span>` : ''}
                    <div class="qr-item-actions">
                        <button class="btn-icon" title="Editar" onclick="window._editQR(${qr.id})">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                        </button>
                        <button class="btn-icon danger" title="Excluir" onclick="window._deleteQR(${qr.id})">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
    }

    function openQRModal(qr = null) {
        editingQRId = qr ? qr.id : null;
        document.getElementById('qrModalTitle').textContent = qr ? 'Editar Mensagem Rapida' : 'Nova Mensagem Rapida';
        document.getElementById('qrShortcut').value = qr ? qr.shortcut : '';
        document.getElementById('qrTitle').value = qr ? qr.title : '';
        document.getElementById('qrContent').value = qr ? qr.content : '';
        document.getElementById('qrCategory').value = qr ? (qr.category || '') : '';
        document.getElementById('qrModalOverlay').style.display = 'flex';
    }

    function closeQRModal() {
        document.getElementById('qrModalOverlay').style.display = 'none';
        editingQRId = null;
    }

    async function handleSaveQR(e) {
        e.preventDefault();
        const payload = {
            shortcut: document.getElementById('qrShortcut').value.trim(),
            title: document.getElementById('qrTitle').value.trim(),
            content: document.getElementById('qrContent').value.trim(),
            category: document.getElementById('qrCategory').value.trim() || null,
        };

        const url = editingQRId ? `/api/quick-replies/${editingQRId}` : '/api/quick-replies';
        const method = editingQRId ? 'PUT' : 'POST';

        const resp = await Auth.apiRequest(url, { method, body: JSON.stringify(payload) });
        if (resp && resp.ok) {
            showToast(editingQRId ? 'Mensagem atualizada' : 'Mensagem criada');
            closeQRModal();
            loadQuickReplies();
        } else {
            const err = await resp.json().catch(() => null);
            showToast(err?.detail || 'Erro ao salvar');
        }
    }

    window._editQR = function (id) {
        const qr = quickReplies.find(q => q.id === id);
        if (qr) openQRModal(qr);
    };

    window._deleteQR = async function (id) {
        if (!confirm('Excluir esta mensagem rapida?')) return;
        const resp = await Auth.apiRequest(`/api/quick-replies/${id}`, { method: 'DELETE' });
        if (resp && resp.ok) {
            showToast('Mensagem excluida');
            loadQuickReplies();
        }
    };

    // ─── Helpers ─────────────────────────────────
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
})();
