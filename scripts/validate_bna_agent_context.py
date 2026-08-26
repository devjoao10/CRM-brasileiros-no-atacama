"""Valida a estrutura do vault de contexto da BIA (bna_agent_context/).

Uso:  python scripts/validate_bna_agent_context.py
Sai com código 0 se tudo OK; 1 se houver falhas. Não acessa rede nem n8n.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "bna_agent_context"

REQUIRED_DIRS = [
    "00_persona", "01_empresa", "02_destinos", "03_tours", "04_precos",
    "05_politicas", "06_saude_seguranca", "07_faq_objecoes",
    "08_operacao_agente", "09_guardrails", "_meta",
]

REQUIRED_FILES = [
    "00_README.md",
    "00_persona/persona_bia.md", "00_persona/tom_de_voz.md",
    "00_persona/exemplos_dialogo.md", "00_persona/proibicoes_de_linguagem.md",
    "01_empresa/empresa.md", "01_empresa/proposta_de_valor.md",
    "01_empresa/canais_atendimento.md",
    "02_destinos/atacama.md", "02_destinos/santiago.md", "02_destinos/uyuni.md",
    "02_destinos/melhor_epoca.md", "02_destinos/logistica_geral.md",
    "04_precos/precos_2026_atacama.md", "04_precos/precos_2026_santiago.md",
    "04_precos/precos_2026_uyuni.md", "04_precos/regras_de_preco.md",
    "04_precos/pendencias_precos.md",
    "05_politicas/pagamento.md", "05_politicas/desconto_pix.md",
    "05_politicas/cancelamento.md", "05_politicas/termos_e_condicoes.md",
    "05_politicas/lgpd_privacidade.md",
    "06_saude_seguranca/altitude.md", "06_saude_seguranca/criancas.md",
    "06_saude_seguranca/idosos.md", "06_saude_seguranca/restricoes_e_cuidados.md",
    "06_saude_seguranca/emergencias.md",
    "07_faq_objecoes/faq_clientes.md", "07_faq_objecoes/objecoes_preco.md",
    "07_faq_objecoes/objecoes_seguranca.md",
    "07_faq_objecoes/objecoes_concorrencia.md", "07_faq_objecoes/quando_escalar.md",
    "08_operacao_agente/fluxo_atendimento_bia.md",
    "08_operacao_agente/campos_obrigatorios_crm.md",
    "08_operacao_agente/handoff_humano.md", "08_operacao_agente/uso_de_tools.md",
    "08_operacao_agente/formato_resposta_whatsapp.md",
    "09_guardrails/nunca_inventar.md", "09_guardrails/nao_negociar.md",
    "09_guardrails/nao_prometer_disponibilidade.md",
    "09_guardrails/dados_sensiveis.md", "09_guardrails/politicas_criticas.md",
    "_meta/schema_frontmatter.md", "_meta/mapa_de_arquivos.md",
    "_meta/checklist_atualizacao.md", "_meta/pendencias_validacao.md",
    "_meta/system_prompt_futuro_curto.md",
    "_meta/pendencias_index.md",
    # Indices de navegacao por pasta (N8N-BIA-GUARDRAILS-03).
    "00_persona/README.md", "01_empresa/README.md", "02_destinos/README.md",
    "03_tours/README.md", "04_precos/README.md", "05_politicas/README.md",
    "06_saude_seguranca/README.md", "07_faq_objecoes/README.md",
    "08_operacao_agente/README.md", "09_guardrails/README.md",
]

MIN_TOUR_FILES = 14

FRONTMATTER_FIELDS = [
    "context_id", "category", "destination", "product", "risk_level",
    "validity", "source", "status", "last_review",
]

# Palavras que indicariam vazamento de segredo (case-insensitive).
SECRET_PATTERNS = [
    r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}",
    r"secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}",
    r"password\s*[:=]",
    r"Bearer\s+[A-Za-z0-9\-_\.]{15,}",
    r"EAA[A-Za-z0-9]{20,}",           # tokens Meta
    r"AIza[A-Za-z0-9\-_]{30,}",        # chaves Google
    r"postgres(ql)?://[^\s]+:[^\s]+@",  # DSN com senha
    r"/webhook(-test)?/[a-z0-9\-]+",    # paths de webhook n8n
]

MAX_FUTURE_PROMPT_CHARS = 8000

# --- checks semanticos cross-file (auditoria 2026-08 — pegam contradicoes
# que o check estrutural acima nao ve: campo divergente, precedencia
# ausente, promessa de e-mail, produto Uyuni inexistente). ---

HANDOFF_FIELD_KEYWORDS = ["nome completo", "destino", "viajante", "email"]
ESCALATION_DISTINCTION_MARKERS = ["handoff comercial", "escalação de limite"]

EMAIL_DELIVERY_RE = re.compile(
    r"e-?mail\s+que\b(?:\s+\w+){0,4}\s+(envio|mando|manda|envia|retorno|retorna|retornamos)\b",
    re.I,
)

UYUNI_BAD_DURATION_RE = re.compile(r"\b1\s*dia\b|\b7\s*dias\b", re.I)
UYUNI_FILES = ["03_tours/uyuni_expedicoes.md", "04_precos/precos_2026_uyuni.md"]


def check_handoff_fields_match(root: Path) -> str | None:
    """campos_obrigatorios_crm.md e handoff_humano.md tem que citar os
    MESMOS 4 campos bloqueantes — se um arquivo desalinhar, a IA que le so
    um dos dois fica com uma lista diferente (causa raiz da H3)."""
    f1 = root / "08_operacao_agente/campos_obrigatorios_crm.md"
    f2 = root / "08_operacao_agente/handoff_humano.md"
    if not f1.is_file() or not f2.is_file():
        return None  # arquivo ausente ja e outra falha (REQUIRED_FILES)
    t1 = f1.read_text(encoding="utf-8").lower()
    t2 = f2.read_text(encoding="utf-8").lower()
    missing1 = [k for k in HANDOFF_FIELD_KEYWORDS if k not in t1]
    missing2 = [k for k in HANDOFF_FIELD_KEYWORDS if k not in t2]
    if missing1 or missing2:
        return (
            "campos obrigatorios de handoff divergem: "
            f"campos_obrigatorios_crm.md sem {missing1}, "
            f"handoff_humano.md sem {missing2}"
        )
    return None


def check_escalation_precedence(root: Path) -> str | None:
    """Se quando_escalar.md manda escalar 'mesmo sem os 4 campos', o
    arquivo precisa deixar explicita a distincao handoff comercial (4
    campos bloqueantes) vs escalacao de limite (bloqueio nao vale) — senao
    o modelo tem duas regras batendo na mesma alavanca (causa raiz da H3)."""
    f = root / "07_faq_objecoes/quando_escalar.md"
    if not f.is_file():
        return None
    text = f.read_text(encoding="utf-8").lower()
    if "mesmo sem os 4 campos" not in text:
        return None
    missing = [m for m in ESCALATION_DISTINCTION_MARKERS if m not in text]
    if missing:
        return (
            "quando_escalar.md cita 'mesmo sem os 4 campos' sem a "
            f"distincao handoff comercial vs escalacao de limite (falta: {missing})"
        )
    return None


def check_no_email_delivery_claim(root: Path) -> list[str]:
    """Nenhum arquivo pode prometer que a cotacao vai 'por e-mail' — ela e
    sempre entregue no WhatsApp; o e-mail e so cadastro no CRM (H6)."""
    problems = []
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        if EMAIL_DELIVERY_RE.search(text):
            rel = str(p.relative_to(root)).replace("\\", "/")
            problems.append(f"{rel}: frase sugere envio da cotacao por e-mail (proibido, ver H6)")
    return problems


def check_no_uyuni_1_or_7_day(root: Path) -> list[str]:
    """uyuni_expedicoes.md/precos_2026_uyuni.md nao podem ofertar Uyuni de
    1 dia nem de 7 dias — esses produtos nao existem (H7). Mencao e ok SE
    for para negar (janela de contexto com 'nao existe' por perto)."""
    problems = []
    for relpath in UYUNI_FILES:
        p = root / relpath
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        for m in UYUNI_BAD_DURATION_RE.finditer(text):
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            window = text[start:end].lower()
            if "não existe" not in window and "nao existe" not in window:
                problems.append(
                    f"{relpath}: possivel oferta de Uyuni '{m.group(0)}' sem negacao proxima"
                )
    return problems


def has_frontmatter(text: str) -> tuple[bool, list[str]]:
    if not text.startswith("---"):
        return False, FRONTMATTER_FIELDS[:]
    end = text.find("\n---", 3)
    if end == -1:
        return False, FRONTMATTER_FIELDS[:]
    block = text[:end]
    missing = [f for f in FRONTMATTER_FIELDS if not re.search(rf"^{f}:", block, re.M)]
    return True, missing


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    if not ROOT.is_dir():
        print(f"FALHA: pasta {ROOT} nao existe")
        return 1

    for d in REQUIRED_DIRS:
        if not (ROOT / d).is_dir():
            failures.append(f"pasta obrigatoria ausente: {d}")

    for f in REQUIRED_FILES:
        if not (ROOT / f).is_file():
            failures.append(f"arquivo obrigatorio ausente: {f}")

    tour_files = list((ROOT / "03_tours").glob("*.md")) if (ROOT / "03_tours").is_dir() else []
    if len(tour_files) < MIN_TOUR_FILES:
        failures.append(f"03_tours tem {len(tour_files)} arquivos (minimo {MIN_TOUR_FILES})")

    all_md = sorted(ROOT.rglob("*"))
    non_md = [p for p in all_md if p.is_file() and p.suffix.lower() != ".md"]
    if non_md:
        failures.append(f"arquivos nao-Markdown no vault: {[str(p.relative_to(ROOT)) for p in non_md]}")

    pendencias_total = 0
    for p in sorted(ROOT.rglob("*.md")):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        text = p.read_text(encoding="utf-8")

        # frontmatter obrigatorio fora de README/_meta
        # READMEs de navegacao (raiz 00_README.md ou <pasta>/README.md) e os
        # arquivos de _meta/ sao indices, nao arquivos de contexto: isentos.
        is_readme = rel == "00_README.md" or rel.endswith("/README.md")
        if not is_readme and not rel.startswith("_meta/"):
            ok, missing = has_frontmatter(text)
            if not ok:
                failures.append(f"{rel}: sem frontmatter")
            elif missing:
                failures.append(f"{rel}: frontmatter sem campos {missing}")

        # varredura de segredos
        for pat in SECRET_PATTERNS:
            m = re.search(pat, text, re.I)
            if m:
                failures.append(f"{rel}: possivel segredo (padrao '{pat}')")

        pendencias_total += text.count("[PENDENTE_VALIDACAO]")

        # consistencia status vs marcador (so em arquivos de contexto; indices
        # README/_meta citam os tokens ao documenta-los e nao tem status proprio)
        if not is_readme and not rel.startswith("_meta/"):
            if "[PENDENTE_VALIDACAO]" in text and 'status: "validado"' in text:
                warnings.append(f"{rel}: contem [PENDENTE_VALIDACAO] mas status=validado (marcadores sao pontuais? conferir)")

    # prompt futuro: existe, nao e gigante, nao tem tabela de precos
    fp = ROOT / "_meta/system_prompt_futuro_curto.md"
    if fp.is_file():
        t = fp.read_text(encoding="utf-8")
        if len(t) > MAX_FUTURE_PROMPT_CHARS + 4000:  # arquivo inclui notas alem do prompt
            failures.append(f"system_prompt_futuro_curto.md muito grande ({len(t)} chars)")
        price_hits = re.findall(r"\d{2,3}\.\d{3}\s*CLP|\d{3,4}\s*USD", t)
        if len(price_hits) > 2:  # tolera 1-2 precos citados em exemplo
            failures.append(f"prompt futuro contem tabela de precos ({len(price_hits)} valores)")

    if (ROOT / "_meta/pendencias_validacao.md").is_file() and pendencias_total == 0:
        warnings.append("nenhum [PENDENTE_VALIDACAO] encontrado — inesperado nesta fase")

    # checks semanticos cross-file (auditoria 2026-08)
    handoff_mismatch = check_handoff_fields_match(ROOT)
    if handoff_mismatch:
        failures.append(handoff_mismatch)

    escalation_problem = check_escalation_precedence(ROOT)
    if escalation_problem:
        failures.append(escalation_problem)

    failures.extend(check_no_email_delivery_claim(ROOT))
    failures.extend(check_no_uyuni_1_or_7_day(ROOT))

    print(f"Arquivos .md: {len(list(ROOT.rglob('*.md')))}")
    print(f"Marcadores [PENDENTE_VALIDACAO]: {pendencias_total}")
    for w in warnings:
        print(f"AVISO: {w}")
    if failures:
        for f in failures:
            print(f"FALHA: {f}")
        print(f"\n{len(failures)} falha(s).")
        return 1
    print("\nOK: estrutura do vault valida.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
