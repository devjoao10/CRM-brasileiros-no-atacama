/**
 * Auth Module — JWT Token Management
 * Handles authentication state for the frontend.
 */

const Auth = {
    TOKEN_KEY: 'crm_access_token',
    USER_KEY: 'crm_user',

    /**
     * Store auth data after login
     */
    setAuth(token, user) {
        localStorage.setItem(this.TOKEN_KEY, token);
        localStorage.setItem(this.USER_KEY, JSON.stringify(user));
    },

    /**
     * Get stored JWT token
     */
    getToken() {
        return localStorage.getItem(this.TOKEN_KEY);
    },

    /**
     * Get stored user data
     */
    getUser() {
        const data = localStorage.getItem(this.USER_KEY);
        return data ? JSON.parse(data) : null;
    },

    /**
     * Check if user is authenticated
     */
    isAuthenticated() {
        return !!this.getToken();
    },

    /**
     * Clear auth data (logout)
     */
    clearAuth() {
        localStorage.removeItem(this.TOKEN_KEY);
        localStorage.removeItem(this.USER_KEY);
    },

    /**
     * Perform authenticated API request
     */
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

        // If 401, redirect to login
        if (response.status === 401) {
            this.clearAuth();
            window.location.href = '/login';
            return null;
        }

        return response;
    },

    /**
     * Logout
     */
    async logout() {
        try {
            await fetch('/api/auth/logout', { method: 'POST' });
        } catch (e) {
            // Ignore
        }
        this.clearAuth();
        window.location.href = '/login';
    },

    /**
     * Redirect to login if not authenticated
     */
    requireAuth() {
        if (!this.isAuthenticated()) {
            window.location.href = '/login';
            return false;
        }
        return true;
    }
};
