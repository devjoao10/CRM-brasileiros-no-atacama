"""Pipeline de retrieval + montagem da resposta fundamentada (grounded).

Retrieval ANTES da geração. Filtra por status de validação: conteúdo
[PENDENTE_VALIDACAO] nunca vira fato confirmado. Retorna fontes internas.
Detecta conflito (validado vs pendente) e no-answer (sem evidência suficiente).

Retrieval HÍBRIDO: combina similaridade de embedding com um PORTÃO LEXICAL de
termos salientes. Um chunk só fundamenta se compartilhar termo saliente com a
pergunta (ou tiver score semântico muito alto). Isso torna o no-answer e o
filtro de pendências robustos e independentes do backend de embeddings — o
determinístico (testes) e o Gemini (produção) têm distribuições de score
diferentes, mas o portão lexical garante que o chunk recuperado realmente toca
o vocabulário da pergunta. NÃO gera a redação final com LLM aqui — monta o
CONTEXTO fundamentado; a redação é da BIA (n8n), sob o guardrail anti-injection.
"""
import re

from . import config
from .embeddings import get_backend
from .store import SqliteVectorStore

_STOPWORDS = {
    "a", "o", "os", "as", "de", "da", "do", "dos", "das", "e", "ou", "que",
    "qual", "quais", "quanto", "quantos", "quanta", "quantas", "com", "sem",
    "para", "por", "no", "na", "nos", "nas", "um", "uma", "uns", "umas", "em",
    "se", "ao", "aos", "the", "is", "como", "vcs", "voces", "voce", "tem",
    "ter", "ha", "sao", "e", "meu", "minha", "seu", "sua", "isso", "esse",
    "essa", "este", "esta", "ele", "ela", "eu", "me", "mais", "menos", "ja",
    "muito", "pouco", "quero", "posso", "pode", "fazer", "faz", "sobre", "qto",
}

# termos salientes fortes que reforçam grounding lexical (domínio BNA)
_TERM_RE = re.compile(r"[a-z0-9áàâãéêíóôõúç]{3,}")


def _salient_terms(text: str) -> set[str]:
    toks = _TERM_RE.findall(text.lower())
    return {t for t in toks if t not in _STOPWORDS}


def _lex_overlap(qterms: set[str], text: str) -> int:
    cterms = _salient_terms(text)
    return len(qterms & cterms)


def _relevant(score: float, overlap: int) -> bool:
    # relevante se: (≥1 termo saliente E score acima do piso), OU overlap
    # lexical forte (≥2 termos salientes) que resgata chunk curto de score baixo,
    # OU score semântico muito alto (sinônimos sem overlap, via Gemini).
    return (overlap >= 1 and score >= config.SCORE_THRESHOLD) or overlap >= 2 or score >= 0.6


def _rank_key(item):
    r = item
    return (1 if r["canonical"] else 0, r["_overlap"], r["score"])


def _dedup_and_cap(results, max_per_file, top_k):
    seen = set()
    per_file = {}
    out = []
    for r in results:
        key = (r["source_path"], r["heading_path"])
        if key in seen:
            continue
        seen.add(key)
        cnt = per_file.get(r["source_path"], 0)
        if cnt >= max_per_file:
            continue
        per_file[r["source_path"]] = cnt + 1
        out.append(r)
        if len(out) >= top_k:
            break
    return out


def _gate(candidates, qterms):
    kept = []
    for r in candidates:
        overlap = _lex_overlap(qterms, r["text"])
        if _relevant(r["score"], overlap):
            r["_overlap"] = overlap
            kept.append(r)
    kept.sort(key=_rank_key, reverse=True)
    return kept


def retrieve(query: str, *, top_k=None, categories=None, include_pending=False,
             db_path=None, backend_name=None, threshold=None) -> dict:
    top_k = top_k or config.DEFAULT_TOP_K
    thr = config.SCORE_THRESHOLD if threshold is None else threshold
    qterms = _salient_terms(query)
    backend = get_backend(backend_name)
    qvec = backend.embed([query])[0]

    store = SqliteVectorStore(db_path=db_path)
    try:
        allowed = [config.STATUS_VALIDATED, config.STATUS_PARTIAL]
        factual_raw = store.search(qvec, top_k * 3, categories=categories,
                                   include_pending=False, allowed_status=allowed)
        pending_raw = store.search(qvec, top_k * 3, categories=categories,
                                   include_pending=True,
                                   allowed_status=[config.STATUS_PENDING])
    finally:
        store.close()

    factual = _dedup_and_cap(_gate(factual_raw, qterms), config.MAX_CHUNKS_PER_FILE, top_k)
    pending = _dedup_and_cap(
        [r for r in _gate(pending_raw, qterms) if r["contains_pending_validation"]],
        config.MAX_CHUNKS_PER_FILE, top_k)

    return _assemble(query, factual, pending)


def _assemble(query, factual, pending) -> dict:
    warnings = []
    sources = [{
        "path": r["source_path"], "heading": r["heading_path"],
        "validation_status": r["validation_status"], "canonical": r["canonical"],
        "score": r["score"],
    } for r in factual]

    has_factual = len(factual) > 0
    has_pending = len(pending) > 0
    conflict = has_factual and has_pending
    if conflict:
        warnings.append(
            "conflito: existe conteudo validado e tambem conteudo pendente de "
            "validacao para esta pergunta; priorize o validado e sinalize a pendencia."
        )

    if has_factual:
        top = factual[0]["score"]
        confidence = "high" if top >= 0.5 else ("medium" if top >= 0.3 else "low")
        return {
            "query": query, "grounded": True, "answerable": True,
            "confidence": confidence, "pending_validation": False, "conflict": conflict,
            "context_chunks": [
                {"path": r["source_path"], "heading": r["heading_path"],
                 "validation_status": r["validation_status"], "canonical": r["canonical"],
                 "score": r["score"], "text": r["text"]} for r in factual],
            "sources": sources,
            "pending_sources": [{"path": r["source_path"], "heading": r["heading_path"]} for r in pending],
            "warnings": warnings, "index_version": config.INDEX_VERSION,
        }

    if has_pending:
        warnings.append(
            "a informacao solicitada existe no contexto mas esta marcada "
            "[PENDENTE_VALIDACAO]: nao apresentar como fato confirmado; escalar "
            "para validacao humana e citar a fonte interna.")
        return {
            "query": query, "grounded": True, "answerable": False,
            "confidence": "low", "pending_validation": True, "conflict": False,
            "context_chunks": [],
            "sources": [{"path": r["source_path"], "heading": r["heading_path"],
                         "validation_status": r["validation_status"],
                         "canonical": r["canonical"], "score": r["score"]} for r in pending],
            "pending_sources": [{"path": r["source_path"], "heading": r["heading_path"]} for r in pending],
            "warnings": warnings, "index_version": config.INDEX_VERSION,
        }

    warnings.append(
        "sem base interna suficiente para responder; a BIA deve informar que "
        "nao encontrou base e escalar para humano — nao inventar.")
    return {
        "query": query, "grounded": False, "answerable": False, "confidence": "low",
        "pending_validation": False, "conflict": False, "context_chunks": [],
        "sources": [], "pending_sources": [], "warnings": warnings,
        "index_version": config.INDEX_VERSION,
    }
