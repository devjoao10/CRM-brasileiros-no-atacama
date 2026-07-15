"""Backends de embeddings.

- GeminiBackend: produção, reusa GEMINI_API_KEY, modelo dedicado
  text-embedding-004 (embedding real, não improvisação com modelo generativo).
- DeterministicBackend: offline/testes/CI. Hashing determinístico em vetor de
  dimensão fixa — SEM rede, SEM API key. Permite provar o pipeline end-to-end
  e rodar toda a suíte sem custo nem efeito externo.

Nenhum backend imprime a API key.
"""
import hashlib
import math
import os
import re

from . import config


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class DeterministicBackend:
    """Embedding determinístico por bag-of-tokens hasheado. Estável entre
    execuções e reinícios (essencial para persistência/testes reproduzíveis).
    Não é semanticamente rico como um modelo real, mas preserva similaridade
    lexical suficiente para validar todo o pipeline (chunking, filtros, fontes,
    no-answer, conflito) de forma hermética."""

    name = "deterministic"

    def __init__(self, dim: int | None = None):
        self.dim = dim or config.DETERMINISTIC_DIM

    def _tokens(self, text: str):
        return re.findall(r"[a-z0-9áàâãéêíóôõúç]+", text.lower())

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            toks = self._tokens(text)
            for tok in toks:
                h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dim
                sign = 1.0 if (h >> 8) & 1 else -1.0
                vec[idx] += sign
                # bigrama de char para um pouco de robustez a variações
                h2 = int(hashlib.sha1(("#" + tok).encode("utf-8")).hexdigest(), 16)
                vec[h2 % self.dim] += 0.5 * sign
            out.append(_normalize(vec))
        return out


class GeminiBackend:
    """Embeddings via Google Generative AI (models/text-embedding-004)."""

    name = "gemini"

    def __init__(self, model: str | None = None):
        self.model = model or config.GEMINI_EMBEDDING_MODEL
        self._configured = False

    def _ensure(self):
        if self._configured:
            return
        api_key = os.getenv(config.GEMINI_API_KEY_ENV, "")
        if not api_key:
            raise RuntimeError(
                f"{config.GEMINI_API_KEY_ENV} nao configurada — backend Gemini indisponivel"
            )
        import google.generativeai as genai  # lazy: só em produção
        genai.configure(api_key=api_key)
        self._genai = genai
        self._configured = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        out = []
        for text in texts:
            res = self._genai.embed_content(model=self.model, content=text)
            emb = res["embedding"] if isinstance(res, dict) else res.embedding
            out.append(_normalize(list(emb)))
        return out


def get_backend(name: str | None = None):
    name = (name or config.EMBEDDING_BACKEND).lower()
    if name == "gemini":
        return GeminiBackend()
    if name == "deterministic":
        return DeterministicBackend()
    raise ValueError(f"backend de embeddings desconhecido: {name}")
