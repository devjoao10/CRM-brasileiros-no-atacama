/**
 * layout.js — Shared layout behavior (auth gate, user info, logout, mobile sidebar)
 * WP-UX-01 / Passo 0 (scaffold).
 *
 * Espelha o boilerplate que hoje está duplicado no <script> de cada página.
 * CONSOME o módulo global `Auth` (static/js/auth.js) — NÃO o altera.
 * Deve ser carregado SEMPRE depois de auth.js.
 *
 * Defensivo: cada elemento é verificado antes de uso, para que a mesma rotina
 * funcione em qualquer página que estenda base.html, sem assumir presença de
 * todos os ids.
 *
 * NOTA: lógica específica de página (ex.: loadAnalytics, CRUD, e as proteções
 * XSS do OP-13 em ai.html/leads.html) NÃO vive aqui — permanece no
 * {% block scripts %} de cada página.
 */

document.addEventListener('DOMContentLoaded', () => {
    // 1) Auth gate — redireciona para /login se não autenticado
    if (typeof Auth === 'undefined' || !Auth.requireAuth()) return;

    // 2) Preencher dados do usuário no topbar
    const user = Auth.getUser();
    if (user) {
        const nameEl = document.getElementById('userName');
        const roleEl = document.getElementById('userRole');
        const avatarEl = document.getElementById('userAvatar');
        if (nameEl) nameEl.textContent = user.nome;
        if (roleEl) roleEl.textContent = user.role === 'admin' ? 'Administrador' : 'Usuário';
        if (avatarEl && user.nome) avatarEl.textContent = user.nome.charAt(0).toUpperCase();
    }

    // 3) Logout
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    }

    // 4) Toggle da sidebar no mobile
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    const menuToggle = document.getElementById('menuToggle');

    if (menuToggle && sidebar) {
        menuToggle.addEventListener('click', () => {
            sidebar.classList.toggle('open');
            if (overlay) overlay.classList.toggle('show');
        });
    }
    if (overlay && sidebar) {
        overlay.addEventListener('click', () => {
            sidebar.classList.remove('open');
            overlay.classList.remove('show');
        });
    }
});
