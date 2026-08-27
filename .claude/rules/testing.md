---
paths:
  - "**/*"
---
# Testing

Test what can break, not every function. Full rubric: `.claude/rules/05-testing.md`.

- Test: business rules, branching logic (CC ≥ 2), money/security/auth, algorithms, edge cases, bug regressions.
- Skip (or integration-only): trivial getters/wrappers, pass-through, config, presentational UI, generated code.
- Rode os arquivos de teste afetados direto (`python tests/test_<nome>.py`) antes de dizer que terminou. Nao existe `pytest` neste projeto.
