# Verification before completion

Before claiming a task is done:
- Test: `python tests/test_<nome>.py` para cada arquivo de teste afetado (um processo por arquivo).
- Nao ha lint nem typecheck configurados: `ruff`, `mypy` e `pytest` NAO sao dependencias deste projeto.

Report the actual command output. Do not assert success without running them.
