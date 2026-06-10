# tests/ — Testes locais (WP-QA-01)

Testes leves, sem rede, sem produção. Provam regressões das correções da
auditoria. Pasta `scratch/` é descartável e **não** versionada.

## Como rodar
```bash
# todos (requer pytest + httpx para o de rate limit)
python -m pytest tests/ -q

# individual, sem pytest:
python tests/test_render_templates.py     # render dos 12 templates + base.html
python tests/test_rate_limit.py           # login 200 + 429 apos exceder 5/min (sobe app em SQLite)
python tests/test_security_greps.py       # greps de seguranca (localhost:5678, seeds, SQL, guards, XSS)
```

## Cobertura
| Teste | Prova |
|---|---|
| `test_render_templates.py` | 12 templates herdam `base.html` (DOCTYPE/sidebar/topbar/active==1/sem localhost) + chat sanitizado |
| `test_rate_limit.py` | WP-SEC-03: rate limit do login efetivo (429 na 6ª tentativa) |
| `test_security_greps.py` | sem `localhost:5678`, sem `admin123`/`create_test_user`, sem `DROP TABLE`/`DELETE FROM` cru, `require_admin` ≥ 13, DOMPurify no chat, `esc()` escapa aspas |

## Dependências
- `test_render_templates.py` / `test_security_greps.py`: só `jinja2` (já no projeto).
- `test_rate_limit.py`: `fastapi.testclient` (requer `httpx`).

## Pendente (próximos WPs de teste)
- Testes de auth/IDOR/HMAC (WP-QA-01 fase 2) — exigem fixtures de banco/seed.
