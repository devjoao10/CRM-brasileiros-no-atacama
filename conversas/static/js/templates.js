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
        loadServiceAvailability();   // CONV-CURATION-01
    });

    // ─── CONV-CURATION-01: curadoria do atendimento ────────────────────
    // APROVADO PELA META != AUTORIZADO PARA FALAR COM CLIENTE. A conta tem
    // alertas de lead, notificacoes de CRM e templates de teste, todos
    // APPROVED. Aqui o admin escolhe quais o atendente pode oferecer.
    //
    // A secao inteira so aparece para admin: a rota responde 403 para os
    // demais e nos escondemos em vez de mostrar um erro. Quem decide e o
    // backend — isto e apresentacao.

    async function loadServiceAvailability() {
        const section = document.getElementById('svcAvailSection');
        const list = document.getElementById('svcAvailList');
        const summary = document.getElementById('svcAvailSummary');

        const resp = await Auth.apiRequest('/api/templates/service-availability');
        if (!resp || resp.status === 403) return;   // usuario comum: secao nem aparece
        section.style.display = '';

        if (!resp.ok) {
            list.textContent = '';
            summary.textContent = 'Não foi possível carregar os templates da Meta. Tente recarregar.';
            summary.className = 'svc-avail-sub warn';
            return;
        }

        const data = await resp.json();
        const templates = data.templates || [];
        summary.className = 'svc-avail-sub' + (data.available === 0 ? ' warn' : '');
        summary.textContent = data.available === 0
            ? `Nenhum template liberado. Enquanto isso, os atendentes não conseguem retomar `
              + `conversas fora da janela de 24h. Marque abaixo os ${templates.length} aprovados que podem ir a um cliente.`
            : `${data.available} de ${templates.length} templates aprovados liberados para o atendimento.`;

        list.textContent = '';
        templates.forEach(t => list.appendChild(svcRowEl(t)));
    }

    function svcRowEl(t) {
        const row = document.createElement('div');
        row.className = 'svc-row';

        const name = document.createElement('span');
        name.className = 'svc-row-name';
        name.textContent = t.name;

        const meta = document.createElement('span');
        meta.className = 'svc-row-meta';
        meta.textContent = `${t.language} · ${t.category || '—'}`;

        const badge = document.createElement('span');
        badge.className = 'svc-badge';
        badge.textContent = t.status;   // sempre APPROVED aqui; explicito mesmo assim

        const body = document.createElement('span');
        body.className = 'svc-row-body';
        body.textContent = t.body_text || '';

        const toggle = document.createElement('label');
        toggle.className = 'svc-toggle';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!t.available;
        const cbText = document.createElement('span');
        cbText.textContent = 'Disponível no atendimento';
        toggle.appendChild(cb);
        toggle.appendChild(cbText);

        cb.addEventListener('change', async () => {
            const wanted = cb.checked;
            toggle.classList.add('busy');
            const resp = await Auth.apiRequest('/api/templates/service-availability', {
                method: 'PUT',
                body: JSON.stringify({ name: t.name, language: t.language, available: wanted }),
            });
            toggle.classList.remove('busy');
            if (resp && resp.ok) {
                t.available = wanted;
                showToast(wanted
                    ? `"${t.name}" liberado para o atendimento`
                    : `"${t.name}" removido do atendimento`);
                loadServiceAvailability();   // resumo e contagem sempre coerentes
            } else {
                cb.checked = !wanted;        // o estado exibido nunca mente sobre o servidor
                showToast('Não foi possível alterar a disponibilidade.');
            }
        });

        row.appendChild(name);
        row.appendChild(meta);
        row.appendChild(badge);
        row.appendChild(body);
        row.appendChild(toggle);
        return row;
    }

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

        // CONV-CURATION-01: reconsulta a Meta ignorando o cache de 5 min
        document.getElementById('btnReloadSvcAvail').addEventListener('click', async () => {
            await Auth.apiRequest('/api/templates/service-availability?refresh=true');
            loadServiceAvailability();
        });

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
            renderParamMapRows();   // CONV-TPLMAP-01: {{n}} adicionado/removido
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
                ? `<span class="template-meta-id" title="Meta ID: ${escapeHtml(t.meta_template_id)}">Meta: ${escapeHtml(String(t.meta_template_id).substring(0, 12))}...</span>`
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
                    <div class="template-category">${categoryLabel} &middot; ${escapeHtml(t.language)}</div>
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

    // ─── CONV-TPLMAP-01: {{n}} -> @VARIAVEL ────────────────────────────
    // Tres conceitos distintos convivem no modal e nao podem se misturar:
    //   {{1}}      -> parametro posicional da Meta
    //   @VARIAVEL  -> variavel interna do Conversas
    //   "Joao"     -> exemplo que a Meta exige para APROVAR
    // Esta secao liga o 1o ao 2o. O 3o continua no campo de exemplos e nunca
    // vira valor de envio.
    //
    // O catalogo vem de /api/variables (fonte de verdade do sistema de
    // variaveis) — NENHUM token escrito neste arquivo, nem em comentario: o
    // teste do pacote falha se algum aparecer, para que ninguem transforme
    // "exemplo no comentario" em "lista fixa no codigo". Backend revalida tudo.

    let variableTokens = [];        // tokens vindos do backend
    let paramMapDraft = {};         // {'1': '@TOKEN'} — selecao atual do modal

    /** Posicoes {{n}} do corpo, unicas e em ordem numerica. */
    function bodyPositions(text) {
        const found = new Set();
        (text || '').replace(/\{\{(\d+)\}\}/g, (m, n) => { found.add(Number(n)); return m; });
        return [...found].filter(n => n >= 1).sort((a, b) => a - b);
    }

    async function loadVariableTokens() {
        if (variableTokens.length) return;
        const resp = await Auth.apiRequest('/api/variables');
        if (!resp || !resp.ok) return;             // sem catalogo: so manual
        const data = await resp.json();
        variableTokens = (data.variables || []).map(v => v.token);
    }

    function renderParamMapRows() {
        const group = document.getElementById('tplParamMapGroup');
        const rows = document.getElementById('tplParamMapRows');
        const positions = bodyPositions(document.getElementById('tplBody').value);

        rows.textContent = '';
        group.style.display = positions.length ? '' : 'none';

        positions.forEach(pos => {
            const row = document.createElement('div');
            row.className = 'form-row';
            row.style.cssText = 'align-items:center; gap:8px; margin-top:6px;';

            const label = document.createElement('code');
            label.textContent = '{{' + pos + '}}';
            label.style.cssText = 'min-width:52px; font-size:12px;';

            const select = document.createElement('select');
            select.dataset.position = String(pos);
            const none = document.createElement('option');
            none.value = '';
            none.textContent = 'Preencher na hora do envio';
            select.appendChild(none);
            variableTokens.forEach(tok => {
                const opt = document.createElement('option');
                opt.value = tok;
                opt.textContent = tok;       // textContent: token nunca vira HTML
                select.appendChild(opt);
            });
            select.value = paramMapDraft[pos] || '';
            select.addEventListener('change', () => {
                if (select.value) paramMapDraft[pos] = select.value;
                else delete paramMapDraft[pos];
            });

            row.appendChild(label);
            row.appendChild(select);
            rows.appendChild(row);
        });

        // Posicao que sumiu do corpo nao pode continuar no rascunho, senao o
        // backend recusaria o PUT inteiro por "posicao nao existe".
        Object.keys(paramMapDraft).forEach(p => {
            if (!positions.includes(Number(p))) delete paramMapDraft[p];
        });
    }

    async function loadParamMap(name, language) {
        paramMapDraft = {};
        if (!name || !language) return;
        const url = `/api/templates/param-map?name=${encodeURIComponent(name)}&language=${encodeURIComponent(language)}`;
        const resp = await Auth.apiRequest(url);
        if (!resp || !resp.ok) return;
        const data = await resp.json();
        paramMapDraft = { ...(data.mappings || {}) };
    }

    /**
     * Salva o mapeamento LOCAL. Nao toca a Meta: o BODY aprovado continua como
     * esta, e por isso trocar de variavel nao exige recriar o template.
     * Falha aqui nao invalida o template ja salvo — avisa e segue.
     */
    async function saveParamMap(name, language) {
        const resp = await Auth.apiRequest('/api/templates/param-map', {
            method: 'PUT',
            body: JSON.stringify({ name, language, mappings: paramMapDraft }),
        });
        if (resp && resp.ok) return true;
        const err = resp ? await resp.json().catch(() => null) : null;
        showToast(err?.detail || 'Template salvo, mas o mapeamento de variaveis falhou.');
        return false;
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

        // CONV-TPLMAP-01: catalogo + mapeamento persistido. Assincrono de
        // proposito — o modal abre na hora e a secao se popula em seguida.
        paramMapDraft = {};
        renderParamMapRows();
        loadVariableTokens()
            .then(() => template ? loadParamMap(template.name, template.language) : null)
            .then(renderParamMapRows);
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
            // CONV-TPLMAP-01: mapeamento e persistido DEPOIS do template, pela
            // chave (name, language) que o backend acabou de confirmar — nunca
            // pelos campos do formulario, que o backend pode ter normalizado.
            await saveParamMap(result.name, result.language);
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
        // AUDIT-2026-08-W2D-orq: so havia o ramo de sucesso. Um 403/409/500 deixava
        // o card na tela sem mensagem nenhuma — indistinguivel de "apagou e a lista
        // ainda nao atualizou". O operador tenta de novo, ou pior, acha que apagou.
        if (!resp) return;                       // 401 ja tratado por Auth.apiRequest
        if (!resp.ok) {
            let detalhe = '';
            try {
                const err = await resp.json();
                detalhe = err && err.detail ? `: ${err.detail}` : '';
            } catch (e) { /* corpo nao-JSON: fica so o status */ }
            showToast(`Nao foi possivel excluir o template (erro ${resp.status})${detalhe}`);
            return;
        }
        const result = await resp.json();
        const metaInfo = result.meta_deleted ? ' (removido do Meta)' : '';
        showToast(`Template excluído${metaInfo}`);
        loadTemplates();
    };

    function escapeHtml(text) {
        // AUDIT-2026-08-W2D-orq: esta copia NAO escapava aspas, ao contrario das
        // outras do repositorio. textContent->innerHTML cobre & < >, mas deixa " e '
        // passarem: um valor "escapado" por ela ainda fechava atributo aspeado.
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
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
})();
