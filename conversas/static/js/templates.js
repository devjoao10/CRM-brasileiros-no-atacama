/**
 * Templates.js — Template management page logic.
 * Full integration with Meta Cloud API for create, sync, submit, and delete.
 */
(function () {
    'use strict';

    let templates = [];
    let editingId = null;

    document.addEventListener('DOMContentLoaded', () => {
        if (!Auth.requireAuth()) return;
        setupEventListeners();
        loadTemplates();
    });

    function setupEventListeners() {
        document.getElementById('btnLogout').addEventListener('click', () => Auth.logout());
        document.getElementById('btnNewTemplate').addEventListener('click', () => openModal());
        document.getElementById('modalClose').addEventListener('click', () => closeModal());
        document.getElementById('btnCancelTemplate').addEventListener('click', () => closeModal());
        document.getElementById('modalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeModal();
        });
        document.getElementById('templateForm').addEventListener('submit', handleSave);

        // Sync button
        document.getElementById('btnSyncTemplates').addEventListener('click', syncTemplates);

        // Filters
        document.getElementById('filterStatus').addEventListener('change', loadTemplates);
        document.getElementById('filterCategory').addEventListener('change', loadTemplates);

        let searchTimer;
        document.getElementById('searchTemplates').addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(loadTemplates, 300);
        });

        // Body char counter
        document.getElementById('tplBody').addEventListener('input', function () {
            document.getElementById('bodyCharCount').textContent = this.value.length;
        });

        // Header type toggle
        document.getElementById('tplHeaderType').addEventListener('change', function () {
            const headerTextGroup = document.getElementById('headerTextGroup');
            headerTextGroup.style.display = this.value === 'TEXT' ? 'block' : 'none';
        });
    }

    async function loadTemplates() {
        const status = document.getElementById('filterStatus').value;
        const category = document.getElementById('filterCategory').value;
        const search = document.getElementById('searchTemplates').value;

        let url = '/api/templates?';
        if (status) url += `status=${status}&`;
        if (category) url += `category=${category}&`;
        if (search) url += `search=${encodeURIComponent(search)}&`;

        const resp = await Auth.apiRequest(url);
        if (!resp || !resp.ok) return;

        const data = await resp.json();
        templates = data.templates || [];
        renderGrid();
    }

    function renderGrid() {
        const grid = document.getElementById('templatesGrid');

        if (templates.length === 0) {
            grid.innerHTML = `
                <div class="empty-state">
                    <svg viewBox="0 0 24 24" width="48" height="48" fill="var(--dark-300)"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zM7 7h10v2H7zm0 4h10v2H7zm0 4h7v2H7z"/></svg>
                    <p>Nenhum template encontrado</p>
                </div>`;
            return;
        }

        grid.innerHTML = templates.map(t => {
            const statusClass = t.status === 'APPROVED' ? 'success' :
                t.status === 'REJECTED' ? 'error' :
                t.status === 'PAUSED' ? 'warning' : 'warning';
            const statusLabel = t.status === 'APPROVED' ? 'Aprovado' :
                t.status === 'REJECTED' ? 'Rejeitado' :
                t.status === 'PAUSED' ? 'Pausado' : 'Pendente';
            const categoryLabel = t.category === 'MARKETING' ? 'Marketing' :
                t.category === 'UTILITY' ? 'Utilidade' : 'Autenticacao';

            const metaInfo = t.meta_template_id
                ? `<span class="template-meta-id" title="Meta ID: ${t.meta_template_id}">Meta: ${t.meta_template_id.substring(0, 12)}...</span>`
                : '<span class="template-meta-id not-synced">Nao submetido ao Meta</span>';

            const rejectionInfo = t.rejection_reason
                ? `<div class="template-rejection">Motivo: ${escapeHtml(t.rejection_reason)}</div>`
                : '';

            return `
                <div class="template-card">
                    <div class="template-card-header">
                        <span class="template-name">${escapeHtml(t.name)}</span>
                        <span class="status-badge ${statusClass}">${statusLabel}</span>
                    </div>
                    <div class="template-category">${categoryLabel} &middot; ${t.language}</div>
                    ${metaInfo}
                    <div class="template-body">${escapeHtml(t.body_text)}</div>
                    ${t.footer_text ? `<div class="template-footer">${escapeHtml(t.footer_text)}</div>` : ''}
                    ${rejectionInfo}
                    <div class="template-card-actions">
                        ${!t.meta_template_id ? `
                            <button class="btn-icon submit" title="Submeter ao Meta para aprovacao" onclick="window._submitTemplate(${t.id})">
                                <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
                            </button>
                        ` : `
                            <button class="btn-icon submit" title="Re-submeter ao Meta" onclick="window._submitTemplate(${t.id})">
                                <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>
                            </button>
                        `}
                        <button class="btn-icon" title="Editar" onclick="window._editTemplate(${t.id})">
                            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                        </button>
                        <button class="btn-icon danger" title="Excluir" onclick="window._deleteTemplate(${t.id})">
                            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                        </button>
                    </div>
                </div>`;
        }).join('');
    }

    function openModal(template = null) {
        editingId = template ? template.id : null;
        document.getElementById('modalTitle').textContent = template ? 'Editar Template' : 'Novo Template';
        document.getElementById('tplName').value = template ? template.name : '';
        document.getElementById('tplCategory').value = template ? template.category : 'UTILITY';
        document.getElementById('tplLanguage').value = template ? template.language : 'pt_BR';
        document.getElementById('tplBody').value = template ? template.body_text : '';
        document.getElementById('tplHeaderType').value = template ? (template.header_type || '') : '';
        document.getElementById('tplHeaderText').value = template ? (template.header_text || '') : '';
        document.getElementById('tplFooter').value = template ? (template.footer_text || '') : '';
        document.getElementById('bodyCharCount').textContent = template ? template.body_text.length : 0;

        // Sample values
        const sampleEl = document.getElementById('tplSampleValues');
        if (template && template.sample_values) {
            sampleEl.value = JSON.stringify(template.sample_values);
        } else {
            sampleEl.value = '';
        }

        // Header text visibility
        document.getElementById('headerTextGroup').style.display =
            (template && template.header_type === 'TEXT') ? 'block' : (template ? 'none' : 'block');

        document.getElementById('modalOverlay').style.display = 'flex';
    }

    function closeModal() {
        document.getElementById('modalOverlay').style.display = 'none';
        editingId = null;
    }

    async function handleSave(e) {
        e.preventDefault();

        const payload = {
            name: document.getElementById('tplName').value.trim(),
            category: document.getElementById('tplCategory').value,
            language: document.getElementById('tplLanguage').value,
            body_text: document.getElementById('tplBody').value.trim(),
            header_type: document.getElementById('tplHeaderType').value || null,
            header_text: document.getElementById('tplHeaderText').value.trim() || null,
            footer_text: document.getElementById('tplFooter').value.trim() || null,
        };

        // Parse sample values
        const sampleText = document.getElementById('tplSampleValues').value.trim();
        if (sampleText) {
            try {
                payload.sample_values = JSON.parse(sampleText);
            } catch (e) {
                showToast('Valores de exemplo inválidos. Use JSON válido.');
                return;
            }
        }

        const url = editingId ? `/api/templates/${editingId}` : '/api/templates';
        const method = editingId ? 'PUT' : 'POST';

        const resp = await Auth.apiRequest(url, {
            method,
            body: JSON.stringify(payload),
        });

        if (resp && resp.ok) {
            const result = await resp.json();
            const msg = editingId ? 'Template atualizado' : 'Template criado';
            if (result.meta_template_id) {
                showToast(`${msg} e submetido ao Meta`);
            } else {
                showToast(msg);
            }
            closeModal();
            loadTemplates();
        } else {
            const err = await resp.json().catch(() => null);
            showToast(err?.detail || 'Erro ao salvar template');
        }
    }

    // ─── Sync with Meta ─────────────────────────
    async function syncTemplates() {
        const btn = document.getElementById('btnSyncTemplates');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" class="spin"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg> Sincronizando...';
        btn.disabled = true;

        try {
            const resp = await Auth.apiRequest('/api/templates/sync', { method: 'POST' });

            if (resp && resp.ok) {
                const result = await resp.json();
                showToast(`Sincronizado: ${result.synced || 0} templates atualizados`);
                loadTemplates();
            } else {
                const err = await resp.json().catch(() => null);
                showToast(err?.detail || 'Erro na sincronização. Verifique as credenciais em Configurações.');
            }
        } catch (e) {
            showToast('Erro ao sincronizar com Meta');
        } finally {
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    }

    // ─── Submit to Meta ─────────────────────────
    window._submitTemplate = async function (id) {
        const t = templates.find(t => t.id === id);
        if (!t) return;

        const action = t.meta_template_id ? 're-submeter' : 'submeter';
        if (!confirm(`Deseja ${action} o template "${t.name}" ao Meta para aprovação?`)) return;

        const resp = await Auth.apiRequest(`/api/templates/${id}/submit`, { method: 'POST' });

        if (resp && resp.ok) {
            const result = await resp.json();
            showToast(`Template "${t.name}" submetido ao Meta (status: ${result.status || 'PENDING'})`);
            loadTemplates();
        } else {
            const err = await resp.json().catch(() => null);
            showToast(err?.detail || 'Erro ao submeter template ao Meta');
        }
    };

    window._editTemplate = function (id) {
        const t = templates.find(t => t.id === id);
        if (t) openModal(t);
    };

    window._deleteTemplate = async function (id) {
        const t = templates.find(t => t.id === id);
        if (!t) return;

        const metaWarning = t.meta_template_id
            ? '\n\nEste template também será removido da conta Meta.'
            : '';
        if (!confirm(`Excluir o template "${t.name}" permanentemente?${metaWarning}`)) return;

        const resp = await Auth.apiRequest(`/api/templates/${id}`, { method: 'DELETE' });
        if (resp && resp.ok) {
            const result = await resp.json();
            const metaInfo = result.meta_deleted ? ' (removido do Meta)' : '';
            showToast(`Template excluído${metaInfo}`);
            loadTemplates();
        }
    };

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
