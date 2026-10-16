"""
OPS-CONV-MEDIA-01 — Guard estatico do docker-compose (prontidao de producao).

Protege os fixes C1/C2 do gate PROD-READINESS-CONVERSAS-01 contra regressao:
  C1: volume PERSISTENTE de midia do Conversas (conversas_media) declarado e
      montado, com CONVERSAS_MEDIA_DIR apontando para o mount.
  C2: META_APP_SECRET passado ao servico conversas (webhook HMAC em prod).
E invariantes de seguranca do compose:
  - segredos SEMPRE por referencia ${VAR} (nunca literal);
  - deploy workflow segue APENAS workflow_dispatch (sem on: push);
  - deploy publica um ref EXPLICITO, sem merge na VPS, sem input interpolado
    em shell e revertendo a arvore se build/up falharem (WP-OPS-03).

Estatico puro (le YAML como texto + parse) — nao executa Docker.
Roda standalone:  python tests/test_infra_compose_guard.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


print("OPS-CONV-MEDIA-01 — guard do docker-compose")

# C1 — volume de midia
check("conversas_media:" in COMPOSE.split("volumes:")[-1],
      "volume nomeado conversas_media declarado no bloco volumes")
check("- conversas_media:/app/data/media" in COMPOSE,
      "servico conversas monta conversas_media em /app/data/media")
check("- CONVERSAS_MEDIA_DIR=/app/data/media" in COMPOSE,
      "CONVERSAS_MEDIA_DIR aponta para o path do volume")

# C2 — META_APP_SECRET por referencia
check("- META_APP_SECRET=${META_APP_SECRET" in COMPOSE,
      "META_APP_SECRET passado ao conversas por REFERENCIA ${...}")

# AUDIT-2026-08-WF2 — item de `environment` com `: ` PRECISA de aspas.
#
# `- VAR=${VAR:-Vendas: Principal}` nao e string para o YAML: o `: ` (dois
# pontos SEGUIDO DE ESPACO) faz o parser ler o item da lista como MAPA, e o
# compose recusa o arquivo inteiro com
# `services.conversas.environment.[7]: unexpected type map[string]interface {}`.
# Foi bloqueador de deploy: o compose ficou invalido em producao.
#
# `${VAR:-...}` sozinho nao dispara nada — ali o dois-pontos vem seguido de `-`,
# nao de espaco. Quem quebra e o espaco no VALOR (`Vendas: Principal`), entao a
# regra e sobre `: `, nao sobre `:`.
#
# Sem PyYAML de proposito: ele nao esta em requirements.txt nem em
# conversas/requirements.txt, e este guard tem de rodar no CI como esta.
_env_sem_aspas = []
for _linha in COMPOSE.splitlines():
    _cru = _linha.strip()
    if not _cru.startswith("- ") or "=" not in _cru:
        continue
    _item = _cru[2:]
    if ": " in _item and not (_item.startswith('"') and _item.endswith('"')):
        _env_sem_aspas.append(_item)
check(not _env_sem_aspas,
      f"todo item de environment com `: ` esta entre aspas — sem elas o YAML le "
      f"a linha como MAPA e o compose recusa o arquivo inteiro "
      f"(violacoes: {_env_sem_aspas})")

# Segredos nunca literais no compose (toda credencial e ${VAR})
secretish = re.findall(r"(SECRET_KEY|META_APP_SECRET|META_ACCESS_TOKEN|POSTGRES_PASSWORD)=([^\s]+)", COMPOSE)
literals = [(k, v) for k, v in secretish if not v.startswith("${")]
check(not literals, f"nenhum segredo LITERAL no compose (violacoes: {[k for k, _ in literals]})")

# Healthcheck em TODO servico de aplicacao (crm e conversas) — sem health o
# Docker nao reinicia container travado e o Traefik segue roteando para ele.
for _svc, _port in (("crm", 8000), ("conversas", 8001)):
    _m = re.search(rf"\n  {_svc}:\n(.*?)(?=\n  \S|\Z)", COMPOSE, re.S)
    check(_m is not None and f"localhost:{_port}/api/health" in _m.group(1),
          f"servico {_svc} tem healthcheck apontando para :{_port}/api/health")

# Deploy continua manual (OPS-01)
check("workflow_dispatch" in WORKFLOW, "deploy.yml tem workflow_dispatch")
on_block = WORKFLOW.split("on:", 1)[1].split("jobs:", 1)[0]
check("push" not in on_block, "deploy.yml SEM gatilho de push (deploy manual)")

# Fail-fast no script SSH. Sem `set -e`, uma falha em `docker compose build`
# seguiria para `up -d` (imagem VELHA + config nova) e o `echo` final ainda
# devolveria exit 0 — deploy parcial com o workflow VERDE.
_script = WORKFLOW.split("script: |", 1)[-1]
_set_e = _script.find("set -e")
check(_set_e != -1, "deploy.yml: script SSH tem `set -e` (fail-fast)")
# WP-OPS-03: `git pull` saiu de proposito (ver o bloco abaixo); publicar e
# `git fetch` + `git checkout --detach` de um ref explicito.
for _cmd in ("git fetch", "git checkout --detach",
             "docker compose build", "docker compose up"):
    check(_set_e != -1 and 0 <= _set_e < _script.find(_cmd),
          f"`set -e` vem ANTES de `{_cmd}`")

# WP-OPS-03 — deploy de ref EXPLICITO, sem merge no servidor.
#
# O run #73 falhou porque /opt/crm estava em HEAD destacado na ponta de
# feat/bia-v2-2026-08-29 (d84c4ce), com docker-compose.yml e requirements.txt
# modificados na VPS, e o `git pull origin main` tentou MESCLAR main nessa linha
# ("Need to specify how to reconcile divergent branches"). A producao nao segue
# `main`: o workflow so publica o ref que o humano digitou (`git fetch` +
# `git checkout --detach`), nunca mescla no servidor e recusa deploy sobre tree
# sujo. Se o pull tivesse passado, teria mesclado as duas linhas em silencio.
#
# Os checks abaixo provam que cada trava BARRA, nao so que o token existe: gate
# apagado, comentado ou sem `exit 1` reprova. Por isso o shell e lido SEM as
# linhas inteiras de comentario, e cada gate e um `if ...; then ... fi` real cuja
# condicao tem a expressao certa e cujo corpo faz `exit 1`.


def _blocos(texto, chave):
    """Corpos indentados de cada `chave:` do YAML (escalar em bloco `|` ou mapa).

    Texto puro, sem PyYAML (ver nota acima): devolve as linhas que vem DEPOIS de
    `chave:` e sao mais indentadas que ela. Escalar na mesma linha
    (`run: echo oi`) entra como um corpo de uma linha.
    """
    linhas = texto.splitlines()
    achados = []
    for i, linha in enumerate(linhas):
        m = re.match(rf"^(\s*(?:-\s+)?){chave}:[ \t]*(.*)$", linha)
        if not m:
            continue
        indent = len(m.group(1))
        corpo = [] if m.group(2).lstrip().startswith(("|", ">")) else [m.group(2)]
        for prox in linhas[i + 1:]:
            if prox.strip() and len(prox) - len(prox.lstrip()) <= indent:
                break
            corpo.append(prox)
        achados.append("\n".join(corpo))
    return achados


def _sem_comentarios(bloco):
    """Bloco de shell sem as linhas INTEIRAS de comentario (`# ...`).

    Sem isto, um gate comentado (`# if [ -n "$SUJO" ]; ...`) ou um comentario que
    cita o comando continuariam contando como "o gate existe".
    """
    return "\n".join(l for l in bloco.splitlines() if not l.lstrip().startswith("#"))


def _gate(bloco, *trechos):
    """True se ha um `if ...; then ... fi` cuja condicao contem TODOS os `trechos`
    e cujo corpo faz `exit 1` - um gate de verdade, nao so o texto da condicao.
    """
    for m in re.finditer(r"\bif\b([^\n]*?);\s*then\b(.*?)\bfi\b", bloco, re.S):
        if all(t in m.group(1) for t in trechos) and re.search(r"\bexit 1\b", m.group(2)):
            return True
    return False


_runs = _blocos(WORKFLOW, "run")
_scripts = _blocos(WORKFLOW, "script")
_m_guard = re.search(r"\n  guard:\n(.*?)(?=\n  \S|\Z)", WORKFLOW, re.S)
_guard = _m_guard.group(1) if _m_guard else ""
_m_deploy = re.search(r"\n  deploy:\n(.*?)(?=\n  \S|\Z)", WORKFLOW, re.S)
_deploy = _m_deploy.group(1) if _m_deploy else ""
_script_sc = _sem_comentarios("\n".join(_scripts))                # script SSH
_guard_sc = _sem_comentarios("\n".join(_blocos(_guard, "run")))   # run do job guard

# Nenhum `git pull` ATIVO no script (comentario que o cite nao conta).
check("git pull" not in _script_sc,
      "WP-OPS-03: script SSH NAO tem `git pull` (nunca mesclar linhas no servidor)")

# `ref` e o que a producao vai rodar: obrigatorio e SEM default. Um valor
# pre-preenchido poderia tirar a producao da linha atual sem ninguem perceber.
_ref_in = (_blocos(on_block, "ref") or [""])[0]
check(re.search(r"^\s*required:\s*true\s*$", _ref_in, re.M) is not None,
      "WP-OPS-03: input `ref` existe em workflow_dispatch e e `required: true`")
_ref_default = re.search(r"^\s*default:[ \t]*(.*?)\s*$", _ref_in, re.M)
check(bool(_ref_in.strip())
      and (_ref_default is None or _ref_default.group(1).strip("'\"") == ""),
      "WP-OPS-03: input `ref` SEM default (valor pre-preenchido moveria a "
      "producao sem aviso)")

# Nenhum input interpolado em shell: a expressao do GitHub dentro de `run:` ou do
# `script:` SSH deixa o texto digitado no input virar codigo. Vale ate em
# COMENTARIO (o GitHub troca a expressao no texto antes do shell ler), por isso
# aqui o bloco e o bruto, sem tirar comentarios. Input entra por `env:`.
_expressoes = [b for b in _runs + _scripts if "${{" in b]
check(bool(_runs) and bool(_scripts) and not _expressoes,
      "WP-OPS-03: nenhum `${{ }}` dentro de `run:` nem do `script:` SSH - input "
      "entra por `env:` " + f"(blocos run: {len(_runs)}, script: {len(_scripts)}, "
      f"com expressao: {len(_expressoes)})")

# Job `guard`: barra input errado ou malicioso ANTES de qualquer SSH.
check("ref_re='^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$'" in _guard_sc
      and _gate(_guard_sc, '! [[ "$DEPLOY_REF" =~ $ref_re ]]', '== *..*', '== *//*',
                '== */ ]]'),
      "WP-OPS-03: guard BARRA (`exit 1`) `ref` fora da regex de allowlist ou com "
      "`..`, `//` ou `/` no fim")
# `HEAD` e `*/HEAD` resolvem `refs/remotes/origin/HEAD` = ponta do branch padrao: o
# deploy sairia da linha atual da producao em silencio (o proprio incidente do run #73).
check(_gate(_guard_sc, '"$DEPLOY_REF" == HEAD', '"$DEPLOY_REF" == */HEAD'),
      "WP-OPS-03: guard BARRA (`exit 1`) `ref` = HEAD ou terminado em /HEAD "
      "(origin/HEAD moveria a producao para o branch padrao)")
# `services`: so nomes minusculos separados por UM espaco (tab, nova linha ou espaco
# duplo degradariam `crm conversas` para `crm` se o transporte nao aspasse o valor);
# depois, cada nome contra a allowlist (so crm e conversas tem build no compose).
check("svc_re='^([a-z]+( [a-z]+)*)?$'" in _guard_sc
      and _gate(_guard_sc, '! [[ "$DEPLOY_SERVICES" =~ $svc_re ]]'),
      "WP-OPS-03: guard BARRA (`exit 1`) `services` que nao sejam nomes minusculos "
      "separados por UM espaco")
check(re.search(r'for \w+ in \$DEPLOY_SERVICES; do\s+case "\$\w+" in\s+crm\|conversas\) ;;'
                r'\s+\*\)(?:(?!\besac\b).)*?\bexit 1\b', _guard_sc, re.S) is not None,
      "WP-OPS-03: guard BARRA (`exit 1`) servico fora da allowlist (crm, conversas)")

# O job `deploy` so roda depois do guard e com a aprovacao do environment.
check(re.search(r"^    needs:\s*guard\s*$", _deploy, re.M) is not None,
      "WP-OPS-03: job deploy tem `needs: guard` (sem ele publica sem validar nada)")
check(re.search(r"^    environment:\s*production\b", _deploy, re.M) is not None,
      "WP-OPS-03: job deploy exige `environment: production` (aprovacao humana)")

# Workflow sem escopo de GITHUB_TOKEN e com UMA execucao por vez (dois disparos
# nunca fazem checkout/build em /opt/crm ao mesmo tempo).
check(re.search(r"^permissions:\s*\{\}\s*$", WORKFLOW, re.M) is not None,
      "WP-OPS-03: workflow com `permissions: {}` (nenhum job precisa de GITHUB_TOKEN)")
check(re.search(r"^concurrency:\s*\n\s+group:\s*deploy-production\s*\n"
                r"\s+cancel-in-progress:\s*false\s*$", WORKFLOW, re.M) is not None,
      "WP-OPS-03: workflow com `concurrency` group deploy-production e "
      "`cancel-in-progress: false` (uma execucao por vez, sem cancelar a em curso)")

# Script SSH. Primeiro o que prova que as envs chegaram; depois a ordem das travas.
_m_sujo = re.search(r"(\w+)=\$\(git status[^\n]*--porcelain[^\n]*--untracked-files=no[^\n]*\)",
                    _script_sc)
_var_sujo = _m_sujo.group(1) if _m_sujo else "SUJO"
_pos_sujo = _m_sujo.start() if _m_sujo else -1
_pos_checkout = _script_sc.find('git checkout --detach "$ALVO"')
check(0 <= _script_sc.find('echo "Alvo: ref=$DEPLOY_REF servicos=${DEPLOY_SERVICES:-todos}"')
      < _pos_sujo,
      "WP-OPS-03: script SSH imprime `Alvo: ref=... servicos=...` ANTES do gate de tree "
      "sujo (a 1a execucao, mesmo abortada, prova que as envs chegaram inteiras)")
check(0 <= _script_sc.find("ANTES_SHA=$(git rev-parse HEAD)") < _pos_checkout,
      "WP-OPS-03: `ANTES_SHA=$(git rev-parse HEAD)` salvo ANTES do checkout (alvo da "
      "reversao)")
# Tree sujo (arquivo RASTREADO alterado) aborta antes de qualquer mudanca; arquivo
# nao rastreado (.env) nao pode travar - por isso `--untracked-files=no`.
check(_m_sujo is not None and _gate(_script_sc, '[ -n "$' + _var_sujo + '" ]')
      and 0 <= _pos_sujo < _pos_checkout,
      "WP-OPS-03: script SSH BARRA (`exit 1`) tree sujo (`git status --porcelain "
      "--untracked-files=no`) ANTES do `git checkout`")
check(re.search(r"git fetch origin --prune '\+refs/heads/\*:refs/remotes/origin/\*'",
                _script_sc) is not None,
      "WP-OPS-03: fetch com refspec explicito (clone de um branch so nao pode "
      "quebrar a resolucao do ref)")
# Alvo = branch de origin; senao SHA hexadecimal em minusculas. O `rev-parse` poe
# ref LOCAL (tag ou branch com nome hexadecimal, ex. `dead`) na frente do SHA
# abreviado, entao o resultado so vale se COMECAR pelo que foi digitado.
check(re.search(r"""REF_LC=\$\(printf '%s' "\$DEPLOY_REF" \| tr 'A-F' 'a-f'\)""", _script_sc)
      and "''|*[!0-9a-f]*)" in _script_sc
      and '"$DEPLOY_REF^{commit}"' not in _script_sc,
      "WP-OPS-03: fallback de SHA so para hexadecimal em minusculas (ref local velho "
      "da VPS nao pode virar alvo)")
check(re.search(r"""case "\$ALVO" in\s+"\$REF_LC"\*\)\s*;;\s+\*\)\s*ALVO=(''|"")\s*;;""",
                _script_sc) is not None
      and '"$REF_LC^{commit}"' in _script_sc,
      "WP-OPS-03: fallback de SHA so aceita o resultado se `$ALVO` comecar por "
      "`$REF_LC` (tag/branch hexadecimal local publicaria outro commit)")
check(_gate(_script_sc, '[ -z "$ALVO" ]'),
      "WP-OPS-03: script SSH aborta (`exit 1`) quando o alvo nao resolve")
# O checkout e sempre do SHA RESOLVIDO (ou da reversao), nunca do `$DEPLOY_REF` cru.
_checkouts = re.findall(r"git checkout\b[^\n]*", _script_sc)
check(_pos_checkout >= 0
      and all("DEPLOY_REF" not in c and ('"$ALVO"' in c or '"$ANTES_SHA"' in c)
              for c in _checkouts),
      "WP-OPS-03: o checkout e do SHA resolvido (`\"$ALVO\"`) ou da reversao "
      "(`\"$ANTES_SHA\"`), nunca do `$DEPLOY_REF` cru")
# Falhou DEPOIS do checkout (build/up)? O trap devolve a arvore ao SHA do "Antes".
_trap = re.search(r"trap '([^']*)' EXIT", _script_sc)
_corpo_trap = _trap.group(1) if _trap else ""
check(_trap is not None and _trap.start() > _pos_checkout >= 0,
      "WP-OPS-03: trap EXIT instalado DEPOIS do checkout do alvo (antes dele nao ha "
      "o que reverter)")
check('rc=$?' in _corpo_trap and re.search(r'"\$rc" -(?:ne|eq) 0', _corpo_trap)
      and 'git checkout --detach "$ANTES_SHA"' in _corpo_trap
      and "docker compose ps" in _corpo_trap,
      "WP-OPS-03: trap reverte a arvore para `$ANTES_SHA` so com status != 0 e manda "
      "conferir `docker compose ps`")

# `with: envs` e o que leva DEPLOY_REF/DEPLOY_SERVICES do step ate o shell remoto.
_envs = re.search(r"^\s*envs:[ \t]*(.*?)\s*$", WORKFLOW, re.M)
check(_envs is not None
      and {"DEPLOY_REF", "DEPLOY_SERVICES"} <= set(_envs.group(1).split(",")),
      "WP-OPS-03: step SSH repassa DEPLOY_REF e DEPLOY_SERVICES via `with: envs`")

# .env.example documenta os NOMES (sem valores reais)
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
check("CONVERSAS_MEDIA_DIR" in ENV_EXAMPLE, ".env.example documenta CONVERSAS_MEDIA_DIR")
check("META_APP_SECRET" in ENV_EXAMPLE, ".env.example documenta META_APP_SECRET")

if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nGUARD DE INFRA OK")
