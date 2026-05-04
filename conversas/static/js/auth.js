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

    async logout() {
        this.clearAuth();
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
