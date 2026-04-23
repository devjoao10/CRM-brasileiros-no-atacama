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
            const type = passwordInput.type === 'password' ? 'text' : 'password';
            passwordInput.type = type;
            togglePassword.textContent = type === 'password' ? '👁️' : '🙈';
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
        alertBox.className = `alert alert-${type} login-alert`;
        alertBox.classList.remove('hidden');
    }

    function hideAlert() {
        alertBox.classList.add('hidden');
    }
});
