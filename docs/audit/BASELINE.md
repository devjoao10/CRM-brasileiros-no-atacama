# BASELINE — quality gates (pre-fix)

Commit: d4831486b767988ed2b91518167d8c50fbeb636e (HEAD of main at audit start)
Branch: audit/full-system-stabilization-2026-08-24
Host: Windows 11, CPython 3.13.5 (production runs 3.11 CRM / 3.12 conversas)

## Canonical commands discovered in the repo
- Unit/integration: .github/workflows/test.yml runs ONE PROCESS PER FILE: `python tests/test_x.py`, split into two jobs by `grep -L/-l CONVERSAS_DIR tests/test_*.py`. No pytest, no pytest.ini/pyproject/setup.cfg/conftest.py anywhere.
- Lint: NONE (no ruff/flake8/black/eslint config anywhere in the repo).
- Typecheck: NONE (no mypy/pyright config).
- Build: `docker compose build` (Dockerfile + conversas/Dockerfile).
- E2E: NONE (no playwright/selenium/cypress in the repo).
- Security checks: NONE automated; tests/test_security_greps.py is a grep-based unit test.
- Mutation tests: NONE.

## Result (local reproduction of the CI command)

TOTAL=51 PASS=50 FAIL=1 wall_clock=3640.4s

| file | group | status | rc | sec |
|---|---|---|---|---|
| tests/test_hub.py | crm | FAIL | -99 | 303.4 |
| tests/test_auth_session_consistency.py | crm | PASS | 0 | 41.0 |
| tests/test_conversas_admin_role.py | conversas | PASS | 0 | 12.4 |
| tests/test_conversas_agent_timeout.py | conversas | PASS | 0 | 20.5 |
| tests/test_conversas_assignment_notes.py | conversas | PASS | 0 | 12.2 |
| tests/test_conversas_audio.py | conversas | PASS | 0 | 13.5 |
| tests/test_conversas_auth_local.py | conversas | PASS | 0 | 13.0 |
| tests/test_conversas_hotfix_filters_resp.py | conversas | PASS | 0 | 13.0 |
| tests/test_conversas_inbox_filters.py | conversas | PASS | 0 | 17.2 |
| tests/test_conversas_media_foundation.py | conversas | PASS | 0 | 19.4 |
| tests/test_conversas_media_storage.py | conversas | PASS | 0 | 28.0 |
| tests/test_conversas_media_types.py | conversas | PASS | 0 | 26.4 |
| tests/test_conversas_mobile_pwa.py | conversas | PASS | 0 | 25.1 |
| tests/test_conversas_mobile_responsive_refinement.py | conversas | PASS | 0 | 43.5 |
| tests/test_conversas_notifications.py | conversas | PASS | 0 | 45.8 |
| tests/test_conversas_operational_state.py | conversas | PASS | 0 | 128.0 |
| tests/test_conversas_outbound.py | conversas | PASS | 0 | 93.2 |
| tests/test_conversas_outbound_integrity.py | conversas | PASS | 0 | 225.0 |
| tests/test_conversas_queue.py | conversas | PASS | 0 | 94.2 |
| tests/test_conversas_quick_replies.py | conversas | PASS | 0 | 119.0 |
| tests/test_conversas_security.py | crm | PASS | 0 | 5.0 |
| tests/test_conversas_service_window.py | conversas | PASS | 0 | 179.5 |
| tests/test_conversas_smoke_seed.py | conversas | PASS | 0 | 150.4 |
| tests/test_conversas_tags.py | conversas | PASS | 0 | 76.3 |
| tests/test_conversas_tags_sync.py | conversas | PASS | 0 | 68.4 |
| tests/test_conversas_template_curation.py | conversas | PASS | 0 | 57.0 |
| tests/test_conversas_template_param_map.py | conversas | PASS | 0 | 54.7 |
| tests/test_conversas_template_variables.py | conversas | PASS | 0 | 62.8 |
| tests/test_conversas_variables.py | conversas | PASS | 0 | 71.8 |
| tests/test_conversas_webhook.py | conversas | PASS | 0 | 37.4 |
| tests/test_infra_compose_guard.py | crm | PASS | 0 | 16.2 |
| tests/test_internal_tasks.py | crm | PASS | 0 | 110.0 |
| tests/test_kanban_ui.py | crm | PASS | 0 | 3.6 |
| tests/test_leads_deeplink_pinned.py | crm | PASS | 0 | 178.3 |
| tests/test_leads_destino_filter_dialect.py | crm | PASS | 0 | 18.6 |
| tests/test_leads_keyset_pagination.py | crm | PASS | 0 | 128.2 |
| tests/test_leads_responsavel_atomic.py | crm | PASS | 0 | 102.5 |
| tests/test_leads_segment_drift.py | crm | PASS | 0 | 92.4 |
| tests/test_notifications_ui.py | crm | PASS | 0 | 90.5 |
| tests/test_perf_query_count.py | crm | PASS | 0 | 133.9 |
| tests/test_perpetua_internal_auth.py | crm | PASS | 0 | 98.0 |
| tests/test_perpetua_pdf_generation.py | crm | PASS | 0 | 50.1 |
| tests/test_pipeline_inline_lead_edit.py | crm | PASS | 0 | 33.1 |
| tests/test_pipeline_review_final.py | crm | PASS | 0 | 59.8 |
| tests/test_pipeline_stage_pagination.py | crm | PASS | 0 | 134.7 |
| tests/test_pipeline_target_clear.py | crm | PASS | 0 | 3.9 |
| tests/test_pipeline_target_highlight.py | crm | PASS | 0 | 91.3 |
| tests/test_rate_limit.py | crm | PASS | 0 | 137.9 |
| tests/test_render_templates.py | crm | PASS | 0 | 1.4 |
| tests/test_security_greps.py | crm | PASS | 0 | 1.0 |
| tests/test_segments_sql_count.py | crm | PASS | 0 | 97.9 |

## Baseline failure detail — INVESTIGATED

`tests/test_hub.py` was the only non-zero exit in the local run (rc=-99 = my 300s harness timeout).

**It is NOT a product failure.** Re-run with a 900s budget: exit 0, both assertions pass
("OK test_hub_renders_without_sidebar", "OK test_hub_route_requires_valid_session").

Root cause, proved with `faulthandler.dump_traceback_later`:
`tests/test_hub.py:55 from app.main import app` -> `app/main.py:18` -> `app/routers/ai.py:11
import google.generativeai` -> `google.ai.generativelanguage_v1beta...async_client` ->
`google/api_core/exceptions.py:40` -> `grpc/__init__.py:2325`, which blocks in
`importlib._bootstrap_external._path_stat` (filesystem stat storm) for **36.2 s measured in
isolation** on this host.

Consequences that ARE real (recorded as findings, not baseline noise):
- `app/routers/ai.py` imports the Gemini SDK at MODULE scope and `app/main.py` imports that
  router unconditionally, so every process that touches the app pays the cost: app startup,
  and **43 of the 51 test files**.
- Total suite wall clock 3640 s (~61 min) for 51 files is dominated by this fixed per-process
  cost multiplied by one process per file (the CI design).
- `google-generativeai` is an END-OF-LIFE package (it prints a FutureWarning on every import);
  requirements.txt pins it open-endedly as `>=0.8.2`, so no fix will ever arrive.

**Corrected baseline: 51/51 test files PASS.** Nothing in the suite was red before this audit,
which means every failure observed after the fixes is a regression I introduced and must explain.

## Gates that DO NOT EXIST in this repository
lint, typecheck, E2E, security scanning, static analysis and mutation testing have no
configuration and no runner anywhere in the tree. They cannot regress because they were never
green; they are recorded as coverage gaps, not as failures.
