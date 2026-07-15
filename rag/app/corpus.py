"""Construção do manifest do corpus a partir de bna_agent_context/.

Lê frontmatter, classifica cada arquivo (incluído/excluído + status de
validação) e calcula hashes. NÃO altera nenhum arquivo. NÃO indexa segredos,
PII, backups, .env ou dados de leads (o corpus é só bna_agent_context/, que já
proíbe esses conteúdos por convenção e é varrido pelo validator).
"""
import hashlib
import re
from pathlib import Path

from . import config


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(text: str) -> dict:
    """Extrai o bloco YAML de frontmatter (parser mínimo chave: valor)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    fm = {}
    for line in block.splitlines():
        m = re.match(r'^([a-z_]+):\s*(.*)$', line.strip())
        if m:
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            fm[key] = val
    return fm


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def classify(rel_path: str, frontmatter: dict, body: str) -> tuple[bool, str, str]:
    """Retorna (included_in_rag, validation_status, exclusion_reason)."""
    parts = rel_path.replace("\\", "/").split("/")
    top = parts[0]
    name = parts[-1]

    # Administrativo / navegação: READMEs, _meta, 00_README.md → excluídos do
    # corpus factual (orientam filtros, não são conteúdo comercial).
    if name == "README.md" or name == "00_README.md" or top == "_meta":
        return (False, config.STATUS_ADMIN, "arquivo administrativo/navegacao")

    if top not in config.CONTENT_DIRS:
        return (False, config.STATUS_ADMIN, f"fora dos diretorios de conteudo ({top})")

    fm_status = (frontmatter.get("status") or "").strip()
    has_marker = config.PENDING_MARKER in body

    if fm_status == config.STATUS_PENDING:
        status = config.STATUS_PENDING
    elif fm_status == config.STATUS_VALIDATED and has_marker:
        status = config.STATUS_PARTIAL
    elif fm_status == config.STATUS_VALIDATED:
        status = config.STATUS_VALIDATED
    elif has_marker:
        status = config.STATUS_PENDING
    else:
        # sem status reconhecido e sem marcador — trata conservador como parcial
        status = config.STATUS_PARTIAL
    return (True, status, "")


def build_manifest(corpus_dir: Path | None = None, indexed_at: str = "") -> dict:
    """Varre o corpus e retorna o manifest (dict serializável). Sem paths
    absolutos da máquina — apenas relative_path."""
    root = Path(corpus_dir or config.CORPUS_DIR)
    files = []
    for p in sorted(root.rglob("*.md")):
        rel = str(p.relative_to(root)).replace("\\", "/")
        text = p.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        body = strip_frontmatter(text)
        included, status, reason = classify(rel, fm, body)
        headings = re.findall(r'^(#{1,6})\s+(.*)$', body, re.M)
        files.append({
            "relative_path": rel,
            "category": fm.get("category", ""),
            "destination": fm.get("destination", ""),
            "document_type": "context" if included else "admin",
            "canonical": _is_canonical(rel),
            "validation_status": status,
            "frontmatter_status": fm.get("status", ""),
            "source": fm.get("source", ""),
            "risk_level": fm.get("risk_level", ""),
            "content_hash": _sha256(text),
            "size": len(text.encode("utf-8")),
            "headings": [h[1] for h in headings],
            "pending_markers": body.count(config.PENDING_MARKER),
            "included_in_rag": included,
            "exclusion_reason": reason,
            "indexed_at": indexed_at,
            "chunk_count": 0,  # preenchido pela ingestão
        })
    included = [f for f in files if f["included_in_rag"]]
    return {
        "index_version": config.INDEX_VERSION,
        "collection": config.COLLECTION,
        "indexed_at": indexed_at,
        "total_files": len(files),
        "included_files": len(included),
        "excluded_files": len(files) - len(included),
        "files": files,
    }


# Documentos canônicos por pasta (síntese/consolidação com prioridade sobre
# recortes). Base: auditoria do contexto (GUARDRAILS-03).
_CANONICAL = {
    "06_saude_seguranca/restricoes_e_cuidados.md",
    "09_guardrails/politicas_criticas.md",
    "04_precos/regras_de_preco.md",
    "08_operacao_agente/fluxo_atendimento_bia.md",
}


def _is_canonical(rel: str) -> bool:
    return rel in _CANONICAL
