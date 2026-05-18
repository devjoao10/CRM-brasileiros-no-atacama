/**
 * Login Page Logic
 */

document.addEventListener('DOMContentLoaded', () => {
    // If already logged in, validate token before redirecting
    if (Auth.isAuthenticated()) {
        fetch('/api/auth/me', {
            headers: { 'Authorization': `Bearer ${Auth.getToken()}` }
        }).then(res => {
            if (res.ok) {
                window.location.href = '/dashboard';
            } else {
                // Token expirado ou inválido — limpa e fica no login
                Auth.clearAuth();
            }
        }).catch(() => {
            Auth.clearAuth();
        });
        return;
    }

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
                window.location.href = '/dashboard';
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
