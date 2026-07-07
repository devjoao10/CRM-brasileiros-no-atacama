/**
 * notifications.js — Sino global de notificações (WP-UX-04)
 *
 * Consome a API existente /api/operational/notifications (GET lista,
 * POST /{id}/read, POST /read-all) via Auth.apiRequest. Carregado pelo
 * base.html DEPOIS de auth.js e layout.js; presente em todas as páginas
 * que estendem a base (Comercial, Operacional, Gestão e Hub).
 *
 * Segurança: itens são construídos com createElement/textContent — a
 * mensagem da notificação NUNCA passa por innerHTML (XSS-safe por construção).
 * Defensivo: se o sino não existir na página (ex.: login), não faz nada.
 */

document.addEventListener('DOMContentLoaded', () => {
    if (typeof Auth === 'undefined' || !Auth.isAuthenticated()) return;

    const bell = document.getElementById('notifBell');
    const badge = document.getElementById('notifBadge');
    const dropdown = document.getElementById('notifDropdown');
    const list = document.getElementById('notifList');
    const markAllBtn = document.getElementById('notifMarkAll');
    const wrap = document.getElementById('notifWrap');
    if (!bell || !badge || !dropdown || !list || !wrap) return;

    const POLL_MS = 60000;   // atualiza o badge a cada 60s
    const MAX_ITEMS = 15;    // dropdown mostra as 15 mais recentes

    let notifications = [];

    function formatTime(iso) {
        try {
            return new Date(iso).toLocaleString('pt-BR', {
                day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
            });
        } catch (e) {
            return '';
        }
    }

    function updateBadge() {
        const unread = notifications.filter(n => !n.read_at).length;
        if (unread > 0) {
            badge.textContent = unread > 9 ? '9+' : String(unread);
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function renderList() {
        list.textContent = '';
        if (!notifications.length) {
            const empty = document.createElement('div');
            empty.className = 'notif-empty';
            empty.textContent = 'Sem notificações por aqui.';
            list.appendChild(empty);
            return;
        }
        notifications.slice(0, MAX_ITEMS).forEach(n => {
            const item = document.createElement('div');
            item.className = 'notif-item' + (n.read_at ? '' : ' unread');

            const msg = document.createElement('div');
            msg.className = 'notif-msg';
            msg.textContent = n.message;   // textContent: nunca innerHTML

            const time = document.createElement('div');
            time.className = 'notif-time';
            time.textContent = formatTime(n.created_at);

            item.appendChild(msg);
            item.appendChild(time);

            if (!n.read_at) {
                item.title = 'Clique para marcar como lida';
                item.addEventListener('click', () => markAsRead(n.id));
            }
            list.appendChild(item);
        });
    }

    async function fetchNotifications() {
        try {
            const resp = await Auth.apiRequest('/api/operational/notifications');
            if (!resp || !resp.ok) return;
            notifications = await resp.json();
            updateBadge();
            if (!dropdown.classList.contains('hidden')) renderList();
        } catch (e) {
            // silencioso: sino não pode quebrar a página
        }
    }

    async function markAsRead(id) {
        const resp = await Auth.apiRequest(`/api/operational/notifications/${id}/read`, { method: 'POST' });
        if (resp && resp.ok) {
            const n = notifications.find(x => x.id === id);
            if (n) n.read_at = new Date().toISOString();
            updateBadge();
            renderList();
        }
    }

    async function markAllAsRead() {
        const resp = await Auth.apiRequest('/api/operational/notifications/read-all', { method: 'POST' });
        if (resp && resp.ok) {
            const now = new Date().toISOString();
            notifications.forEach(n => { if (!n.read_at) n.read_at = now; });
            updateBadge();
            renderList();
        }
    }

    function toggleDropdown(open) {
        const willOpen = open !== undefined ? open : dropdown.classList.contains('hidden');
        dropdown.classList.toggle('hidden', !willOpen);
        bell.setAttribute('aria-expanded', String(willOpen));
        if (willOpen) {
            renderList();
            fetchNotifications();   // refresh ao abrir
        }
    }

    bell.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDropdown();
    });

    if (markAllBtn) {
        markAllBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            markAllAsRead();
        });
    }

    // Fecha ao clicar fora ou com Esc
    document.addEventListener('click', (e) => {
        if (!wrap.contains(e.target)) toggleDropdown(false);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') toggleDropdown(false);
    });

    fetchNotifications();
    setInterval(fetchNotifications, POLL_MS);
});
