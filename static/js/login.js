/**
 * Login Page Logic
 */

/**
 * Destino interno seguro do `?next=` — bloqueia open redirect
 * (`?next=https://evil.com`, `?next=//evil.com`, `javascript:`).
 */
function safeNext() {
    const raw = new URLSearchParams(window.location.search).get('next');
    if (!raw) return null;
    try {
        // URL() resolve o valor contra a própria origem: qualquer destino
        // externo (`//evil.com`, `https://evil.com`, `javascript:`) cai em
        // outra origin. Valor malformado cai no catch e vira o destino padrão.
        const url = new URL(raw, window.location.origin);
        return url.origin === window.location.origin ? url.pathname + url.search : null;
    } catch (e) {
        return null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // If already logged in, validate the session before redirecting.
    //
    // AUTH-LOOP-01: valida com a MESMA credencial que as páginas protegidas
    // exigem — o cookie de sessão — e não com o JWT do localStorage. Antes,
    // um localStorage válido + cookie ausente fazia /api/auth/me responder 200
    // e /hub responder 302 para /login, em loop infinito até o 429.
    if (Auth.isAuthenticated()) {
        fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store' })
            .then(res => {
                if (!res.ok) {
                    // Sessão inexistente/expirada — limpa e fica no login.
                    Auth.clearAuth();
                    return;
                }
                if (sessionStorage.getItem(Auth.HOP_KEY)) {
                    // Já tentamos ir para a página protegida e voltamos ao
                    // login: backend e frontend discordam. Zera o estado local
                    // em vez de tentar de novo.
                    Auth.clearAuth();
                    return;
                }
                sessionStorage.setItem(Auth.HOP_KEY, '1');
                window.location.href = safeNext() || '/hub';
            })
            .catch(() => {
                Auth.clearAuth();
            });
        return;
    }
    sessionStorage.removeItem(Auth.HOP_KEY);

    const form = document.getElementById('loginForm');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const submitBtn = document.getElementById('submitBtn');
    const btnText = document.getElementById('btnText');
    const btnSpinner = document.getElementById('btnSpinner');
    const alertBox = document.getElementById('loginAlert');
    const alertMessage = document.getElementById('alertMessage');
    const togglePassword = document.getElementById('togglePassword');

    // Toggle password visibility
    if (togglePassword) {
        togglePassword.addEventListener('click', () => {
            const isPassword = passwordInput.type === 'password';
            passwordInput.type = isPassword ? 'text' : 'password';
            togglePassword.innerHTML = isPassword
                ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`
                : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        });
    }

    // Form submit
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        hideAlert();

        const email = emailInput.value.trim();
        const password = passwordInput.value;

        // Validation
        if (!email || !password) {
            showAlert('Preencha todos os campos', 'error');
            return;
        }

        // Show loading
        setLoading(true);

        try {
            const response = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });

            const data = await response.json();

            if (!response.ok) {
                showAlert(data.detail || 'Erro ao fazer login', 'error');
                setLoading(false);
                return;
            }

            // Store auth data
            Auth.setAuth(data.access_token, data.user);

            // Success animation
            showAlert('Login realizado com sucesso!', 'success');

            // Redirect after brief delay for feedback
            setTimeout(() => {
                sessionStorage.setItem(Auth.HOP_KEY, '1');
                window.location.href = safeNext() || '/hub';
            }, 500);

        } catch (err) {
            showAlert('Erro de conexão. Tente novamente.', 'error');
            setLoading(false);
        }
    });

    // Focus email input
    emailInput.focus();

    // --- Helpers ---

    function setLoading(loading) {
        submitBtn.disabled = loading;
        btnText.textContent = loading ? 'Entrando...' : 'Entrar';
        btnSpinner.classList.toggle('hidden', !loading);
    }

    function showAlert(message, type = 'error') {
        alertMessage.textContent = message;
        alertBox.className = type === 'success' ? 'login-alert login-alert-success' : 'login-alert';
        alertBox.classList.remove('hidden');
    }

    function hideAlert() {
        alertBox.classList.add('hidden');
    }
});
