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
        if rel != "00_README.md" and not rel.startswith("_meta/"):
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

        # consistencia status vs marcador
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
