"""
Fonte única de verdade do rate limiter (WP-SEC-03).

Antes existiam DUAS instâncias de `Limiter` (uma em app/main.py, outra em
app/routers/auth.py). O decorator `@limiter.limit(...)` do login usava uma
instância e `app.state.limiter` outra; além disso faltava `SlowAPIMiddleware`,
então o `default_limits` global nunca era aplicado.

Agora `main.py` e `auth.py` importam ESTA instância única. `main.py` registra
`app.state.limiter = limiter`, o handler de `RateLimitExceeded` e o
`SlowAPIMiddleware` (que aplica o teto global por IP).
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# default_limits = teto global por IP, aplicado via SlowAPIMiddleware (main.py).
# Limites por rota (ex.: login "5/minute") são adicionados com @limiter.limit.
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
