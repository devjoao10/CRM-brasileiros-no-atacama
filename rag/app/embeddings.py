"""Backends de embeddings (BIA-RAG-EMBEDDINGS-01).

Migração: o SDK antigo `google.generativeai` + modelo `text-embedding-004` foi
descontinuado (404 na API v1beta). Agora usamos o SDK oficial `google-genai`
(`from google import genai`) com o modelo `gemini-embedding-001`.

Contratos SEPARADOS documento/pergunta (task types distintos):
- embed_documents(texts): task_type RETRIEVAL_DOCUMENT
- embed_query(text):      task_type QUESTION_ANSWERING (fallback RETRIEVAL_QUERY)

Dimensionalidade fixa (768) igual na ingestão e na consulta. Vetores são
normalizados (Python puro) antes de armazenar e de comparar — gemini-embedding-001
com dimensionalidade reduzida NÃO garante normalização automática.

- GeminiBackend: produção. Reusa GEMINI_API_KEY (nunca impressa). Cliente
  injetável (testes usam fake, sem rede). Batching validado; retry só em falhas
  transitórias (429/5xx); nunca em 400/401/403/404.
- DeterministicBackend: offline/testes. Hashing determinístico, sem rede.
"""
import hashlib
import math
import os
import re
import time

from . import config


def normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Backend determinístico (offline / testes) — sem rede, sem API key.
# ---------------------------------------------------------------------------
class DeterministicBackend:
    name = "deterministic"
    provider = "deterministic"
    model = "deterministic-hash-v1"

    def __init__(self, dim=None):
        self.dim = dim or config.DETERMINISTIC_DIM
        self.document_task_type = "DETERMINISTIC"
        self.query_task_type = "DETERMINISTIC"

    def _tokens(self, text):
        return re.findall(r"[a-z0-9áàâãéêíóôõúç]+", text.lower())

    def _embed_one(self, text):
        vec = [0.0] * self.dim
        for tok in self._tokens(text):
            h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[h % self.dim] += sign
            h2 = int(hashlib.sha1(("#" + tok).encode("utf-8")).hexdigest(), 16)
            vec[h2 % self.dim] += 0.5 * sign
        return normalize(vec)

    def embed_documents(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text):
        return self._embed_one(text)

    def describe(self):
        return {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_dimensions": self.dim,
            "document_task_type": self.document_task_type,
            "query_task_type": self.query_task_type,
        }

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Erros / classificação de status
# ---------------------------------------------------------------------------
class EmbeddingError(RuntimeError):
    """Erro de embedding com mensagem segura (nunca inclui o texto do chunk)."""


_TRANSIENT = {429, 500, 502, 503, 504}
_NEVER_RETRY = {400, 401, 403, 404}


def status_code_of(exc):
    """Extrai o status HTTP de uma exceção do SDK (google.genai.errors.APIError
    expõe .code; caimos para .status_code ou parsing do texto)."""
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Backend Gemini (produção) — SDK google-genai
# ---------------------------------------------------------------------------
class GeminiBackend:
    name = "gemini"
    provider = "gemini"

    def __init__(self, client=None, model=None, dims=None, document_task_type=None,
                 query_task_type=None, query_fallback_task_type=None,
                 batch_size=None, max_retries=3, backoff_base=0.5, sleep_fn=None):
        self.model = model or config.EMBEDDING_MODEL
        self.dims = int(dims or config.EMBEDDING_DIMENSIONS)
        self.document_task_type = document_task_type or config.DOCUMENT_TASK_TYPE
        self.query_task_type = query_task_type or config.QUERY_TASK_TYPE
        self.query_fallback_task_type = query_fallback_task_type or config.QUERY_FALLBACK_TASK_TYPE
        self.batch_size = int(batch_size or config.EMBEDDING_BATCH_SIZE)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep_fn or time.sleep
        self._client = client  # injeção para testes
        self._types = None
        self.last_task_types = []  # observabilidade de teste (não sensível)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv(config.GEMINI_API_KEY_ENV, "")
        if not api_key:
            raise EmbeddingError(
                f"{config.GEMINI_API_KEY_ENV} nao configurada — backend Gemini indisponivel"
            )
        from google import genai  # lazy: só em produção
        self._client = genai.Client(api_key=api_key)
        return self._client

    def _config(self, task_type):
        if self._types is None:
            try:
                from google.genai import types
                self._types = types
            except Exception:  # noqa: BLE001
                self._types = False
        if self._types:
            return self._types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=self.dims)
        # fallback (fake client nos testes aceita dict)
        return {"task_type": task_type, "output_dimensionality": self.dims}

    def _call(self, contents, task_type):
        """Uma chamada à API com retry seletivo. Retorna lista de vetores."""
        client = self._ensure_client()
        self.last_task_types.append(task_type)
        attempt = 0
        while True:
            try:
                resp = client.models.embed_content(
                    model=self.model, contents=contents, config=self._config(task_type))
                embs = getattr(resp, "embeddings", None)
                if embs is None:
                    raise EmbeddingError("resposta de embedding sem campo 'embeddings'")
                return [list(getattr(e, "values", e)) for e in embs]
            except EmbeddingError:
                raise
            except Exception as exc:  # noqa: BLE001
                code = status_code_of(exc)
                if code not in _TRANSIENT:
                    # 400/401/403/404/config inválida/desconhecido: NÃO retenta.
                    # Mensagem segura: só tipo + status, nunca o texto enviado.
                    raise EmbeddingError(
                        f"falha de embedding (status={code}, {type(exc).__name__}) — sem retry"
                    ) from None
                attempt += 1
                if attempt > self.max_retries:
                    raise EmbeddingError(
                        f"falha transitoria persistente (status={code}) apos {self.max_retries} tentativas"
                    ) from None
                self._sleep(self.backoff_base * (2 ** (attempt - 1)))

    def _embed_batched(self, texts, task_type):
        out = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            vectors = self._call(batch, task_type)
            if len(vectors) != len(batch):
                raise EmbeddingError(
                    f"inconsistencia: {len(batch)} textos enviados, {len(vectors)} vetores recebidos"
                )
            for v in vectors:
                if len(v) != self.dims:
                    raise EmbeddingError(
                        f"dimensionalidade inesperada: {len(v)} (esperado {self.dims})"
                    )
            out.extend(normalize(v) for v in vectors)
        return out

    def embed_documents(self, texts):
        if not texts:
            return []
        return self._embed_batched(list(texts), self.document_task_type)

    def embed_query(self, text):
        try:
            return self._embed_batched([text], self.query_task_type)[0]
        except EmbeddingError as exc:
            # fallback explícito e testado: se o task_type de pergunta for
            # incompativel na conta (400/404), usar RETRIEVAL_QUERY uma vez.
            if ("status=400" in str(exc) or "status=404" in str(exc)) and \
               self.query_fallback_task_type and \
               self.query_fallback_task_type != self.query_task_type:
                return self._embed_batched([text], self.query_fallback_task_type)[0]
            raise

    def describe(self):
        return {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_dimensions": self.dims,
            "document_task_type": self.document_task_type,
            "query_task_type": self.query_task_type,
        }

    def close(self):
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def get_backend(name=None, **kwargs):
    name = (name or config.EMBEDDING_BACKEND).lower()
    if name == "gemini":
        return GeminiBackend(**kwargs)
    if name == "deterministic":
        return DeterministicBackend()
    raise ValueError(f"backend de embeddings desconhecido: {name}")
