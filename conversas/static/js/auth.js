/**
 * Auth Module — JWT Token Management
 * Shared with the CRM. Handles authentication state for the frontend.
 */

const CRM_BASE_URL = window.location.hostname === 'localhost'
    ? 'http://localhost:8000'
    : 'https://crm.crmbrasileirosnoatacama.cloud';

const Auth = {
    TOKEN_KEY: 'crm_access_token',
    USER_KEY: 'crm_user',

    setAuth(token, user) {
        localStorage.setItem(this.TOKEN_KEY, token);
        localStorage.setItem(this.USER_KEY, JSON.stringify(user));
    },

    getToken() {
        return localStorage.getItem(this.TOKEN_KEY);
    },

    getUser() {
        const data = localStorage.getItem(this.USER_KEY);
        return data ? JSON.parse(data) : null;
    },

    isAuthenticated() {
        return !!this.getToken();
    },

    clearAuth() {
        localStorage.removeItem(this.TOKEN_KEY);
        localStorage.removeItem(this.USER_KEY);
    },

    async apiRequest(url, options = {}) {
        const token = this.getToken();
        const headers = {
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        };

        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const response = await fetch(url, {
            ...options,
            headers,
        });

        if (response.status === 401) {
            this.clearAuth();
            window.location.href = '/login';
            return null;
        }

        return response;
    },

    // AUDIT-2026-08-W1B — F4: `clearAuth()` so apaga o localStorage. O cookie de
    // sessao vive no servidor (HttpOnly, invisivel para este script), entao sem
    // chamar /api/auth/logout o "sair" era cosmetico: a credencial continuava
    // valida por 8h e qualquer aba/pessoa na mesma maquina voltava para dentro.
    // So redireciona se o servidor CONFIRMOU o encerramento — mandar para /login
    // com a sessao viva e mentir para o usuario dizendo que ele saiu.
    async logout() {
        let encerrouNoServidor = false;
        try {
            const response = await fetch('/api/auth/logout', {
                method: 'POST',
                credentials: 'same-origin',
            });
            encerrouNoServidor = response.ok;
        } catch (err) {
            console.error('[auth] falha ao encerrar a sessao no servidor:', err);
        }

        this.clearAuth();

        if (!encerrouNoServidor) {
            alert('Nao foi possivel encerrar a sessao no servidor. '
                + 'Verifique a conexao e tente sair novamente.');
            return;
        }
        window.location.href = '/login';
    },

    requireAuth() {
        if (!this.isAuthenticated()) {
            window.location.href = '/login';
            return false;
        }
        return true;
    }
};
