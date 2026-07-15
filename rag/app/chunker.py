"""Chunking semântico por estrutura de headings (não por tamanho fixo).

Mantém título junto do conteúdo; não separa item de política da condição, nem
preço da unidade, nem restrição da explicação — porque quebra por SEÇÃO
(heading), agrupando parágrafos/listas/tabelas da mesma seção. Seções grandes
são subdivididas por parágrafo com overlap pequeno, preservando o heading_path.
"""
import hashlib
import re

from . import config
from .corpus import parse_frontmatter, strip_frontmatter


def estimate_tokens(text: str) -> int:
    """Estimativa barata de tokens (~1 token por 4 chars, mínimo por palavras)."""
    words = len(text.split())
    return max(words, len(text) // 4)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_sections(body: str):
    """Divide o corpo em seções [(heading_path, texto)] por headings markdown."""
    lines = body.splitlines()
    sections = []
    stack = []  # (level, title)
    buf = []
    cur_heading_path = "(intro)"

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append((cur_heading_path, text))

    for line in lines:
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            flush()
            buf.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_heading_path = " > ".join(t for _, t in stack)
            buf.append(line)  # mantém o heading dentro do chunk
        else:
            buf.append(line)
    flush()
    return sections


def _split_large(text: str, heading_line: str):
    """Subdivide um texto grande por parágrafos, com overlap, mantendo o
    heading no topo de cada sub-chunk."""
    paras = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks = []
    cur = []
    cur_tokens = 0
    for p in paras:
        pt = estimate_tokens(p)
        if cur and cur_tokens + pt > config.CHUNK_MAX_TOKENS:
            chunks.append("\n\n".join(cur))
            # overlap: mantém o último parágrafo como início do próximo
            overlap = cur[-1] if estimate_tokens(cur[-1]) <= config.CHUNK_OVERLAP_TOKENS * 2 else ""
            cur = [overlap] if overlap else []
            cur_tokens = estimate_tokens(overlap) if overlap else 0
        cur.append(p)
        cur_tokens += pt
    if cur:
        chunks.append("\n\n".join(cur))
    # garante heading no topo de cada sub-chunk (se não estiver)
    out = []
    for c in chunks:
        if heading_line and not c.lstrip().startswith("#"):
            out.append(f"{heading_line}\n{c}")
        else:
            out.append(c)
    return out


def chunk_document(rel_path: str, text: str, file_meta: dict) -> list[dict]:
    """Gera chunks com metadados para um documento. file_meta vem do manifest."""
    fm = parse_frontmatter(text)
    body = strip_frontmatter(text)
    content_hash = _sha256(text)
    sections = _split_sections(body)

    chunks = []
    idx = 0
    for heading_path, sec_text in sections:
        heading_line = sec_text.splitlines()[0] if sec_text.lstrip().startswith("#") else ""
        pieces = [sec_text]
        if estimate_tokens(sec_text) > config.CHUNK_MAX_TOKENS:
            pieces = _split_large(sec_text, heading_line)
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            contains_pending = config.PENDING_MARKER in piece
            # status do chunk: se o chunk contém marcador, é pendente (mais
            # restritivo que o arquivo); senão herda o status do arquivo.
            file_status = file_meta.get("validation_status", config.STATUS_PARTIAL)
            if contains_pending:
                chunk_status = config.STATUS_PENDING
            else:
                chunk_status = file_status
            chunk = {
                "source_path": rel_path,
                "category": fm.get("category", file_meta.get("category", "")),
                "destination": fm.get("destination", ""),
                "title": (file_meta.get("headings") or [rel_path])[0] if file_meta.get("headings") else rel_path,
                "heading_path": heading_path,
                "validation_status": chunk_status,
                "file_validation_status": file_status,
                "canonical": bool(file_meta.get("canonical")),
                "content_hash": content_hash,
                "chunk_index": idx,
                "contains_pending_validation": contains_pending,
                "language": "pt-BR",
                "tags": [t for t in [fm.get("category"), fm.get("destination")] if t],
                "text": piece,
            }
            chunk["chunk_hash"] = _sha256(f"{rel_path}::{idx}::{piece}")
            chunks.append(chunk)
            idx += 1
    return chunks
