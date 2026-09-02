"""Local-model fallback: offline embeddings and generation.

Built per the stack research notes (all primary-sourced):
- Embeddings: Qwen3-Embedding-0.6B via fastembed 0.8.0's native int8
  build (``Qwen/Qwen3-Embedding-0.6B-Q``) on onnxruntime - the
  research-verified default for CPU. Instructions: queries carry
  'Instruct: ...\\nQuery: ...', documents carry nothing (the card's
  own format; skipping the query instruct costs 1-5% retrieval).
- Generation: Qwen3.5-4B GGUF (Q4_K_M) via llama.cpp - the research's
  new default local-QA model (Feb 2026, Apache-2.0, 262k ctx).

Both classes are LAZY: model weights load on first use, never at
import, so the module costs nothing in the test suite and the
engine. Weights are NOT bundled and NOT auto-downloaded - the user
opts in (a doctor check reports whether the local stack is
available). The provider switch picks per request.

The honest contract, same as the research: a local fallback runs the
SAME answer ladder - INSUFFICIENT stays INSUFFICIENT; the badge just
says which engine answered.
"""

from __future__ import annotations

import os


class LocalModelError(Exception):
    """The local stack is not installed/downloaded - the fix is stated."""


# ---- embeddings ---------------------------------------------------------------

LOCAL_EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B-Q"   # fastembed int8 build
LOCAL_EMBED_DIMS = 1024                              # native; MRL allows 32-1024

_QUERY_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query"
)


class LocalEmbedder:
    """Qwen3-Embedding-0.6B (int8 ONNX) - the offline embedder.

    Same protocol as GeminiEmbedder: ``embed_documents(list[str]) ->
    list[bytes]`` (fp32 blobs). Lazy import of fastembed; missing
    package or weights raise LocalModelError with the fix stated, at
    CALL time, never import time.

    Dimension note: this embedder emits 1024-dim vectors, NOT the API
    model's 1536. The two spaces are incompatible by design - switching
    embedding models re-embeds the corpus (the identity key encodes
    model+dims, so a switch produces a new representation generation,
    not silent mixed-space search). The provider switch therefore
    applies to BOTH query-side and corpus-side together.
    """

    name = "local-qwen3-emb"

    def __init__(self, model: str | None = None, cache_dir: str | None = None):
        self._model_name = model or LOCAL_EMBED_MODEL
        self._cache = cache_dir
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise LocalModelError(
                "fastembed is not installed. Local embeddings need it:\n"
                "  pip install fastembed\n"
                "(weights download on first use, ~600 MB)"
            ) from e
        try:
            self._model = TextEmbedding(
                model_name=self._model_name, cache_dir=self._cache
            )
        except Exception as e:  # noqa: BLE001 - fastembed raises varied types
            raise LocalModelError(
                f"could not load {self._model_name}: {e}\\n"
                "Local embeddings require the model weights (first use "
                "downloads them; offline machines must pre-cache)."
            ) from e

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        self._ensure_model()
        # document side: NO instruct prefix (the card's format)
        vectors = list(self._model.embed(texts))
        import struct

        return [struct.pack(f"{len(v)}f", *v) for v in vectors]

    def embed_query(self, query: str) -> bytes:
        """Query side carries the task instruct (card format, 1-5% quality)."""
        self._ensure_model()
        vectors = list(
            self._model.embed([f"Instruct: {_QUERY_INSTRUCT}\nQuery: {query}"])
        )
        import struct

        v = vectors[0]
        return struct.pack(f"{len(v)}f", *v)


# ---- generation ----------------------------------------------------------------

LOCAL_GENERATE_MODEL_ENV = "FENRIS_LOCAL_MODEL"
LOCAL_GENERATE_DEFAULT = "Qwen3.5-4B-Q4_K_M.gguf"   # research default pick


class LocalGenerator:
    """Qwen3.5-4B (Q4_K_M GGUF) via llama.cpp - the offline answerer.

    Implements the same two-method protocol as GeminiGenerator
    (generate / generate_stream). Lazy: nothing loads until the first
    call. The model file comes from FENRIS_LOCAL_MODEL (a .gguf path)
    or a well-known models directory; missing llama-cpp or the weights
    raise LocalModelError with the fix stated.
    """

    name = "local-llamacpp"

    def __init__(self, model_path: str | None = None, ctx: int = 8192):
        self._model_path = model_path
        self._ctx = ctx
        self._llm = None
        self.calls: list[str] = []

    @staticmethod
    def resolve_model_path() -> str | None:
        """Where the .gguf lives: env override, then ./models/."""
        p = os.environ.get(LOCAL_GENERATE_MODEL_ENV)
        if p and os.path.exists(p):
            return p
        from pathlib import Path

        local = Path("models") / LOCAL_GENERATE_DEFAULT
        if local.exists():
            return str(local)
        return None

    def _ensure_model(self):
        if self._llm is not None:
            return
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise LocalModelError(
                "llama-cpp-python is not installed. Local answers need it:\\n"
                "  pip install llama-cpp-python --extra-index-url "
                "https://abetlen.github.io/llama-cpp-python/whl/cpu\\n"
                "(CPU wheels; or vendor llama.cpp as a sidecar in the "
                "desktop build)"
            ) from e
        path = self._model_path or self.resolve_model_path()
        if not path:
            raise LocalModelError(
                f"no local model file. Set {LOCAL_GENERATE_MODEL_ENV} to a "
                ".gguf path (research default: Qwen3.5-4B Q4_K_M, ~2.7 GB) "
                "or place models/{LOCAL_GENERATE_DEFAULT}."
            )
        self._llm = Llama(model_path=path, n_ctx=self._ctx, verbose=False)

    def generate(self, prompt: str):
        """One structured generation via the model's chat template."""
        from fenris.generate.generator import parse_structured

        self.calls.append(prompt)
        self._ensure_model()
        try:
            out = self._llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.2,
            )
            text = out["choices"][0]["message"]["content"] or ""
            return parse_structured(text)
        except LocalModelError:
            raise
        except Exception as e:  # noqa: BLE001 - mirror GeminiGenerator's contract
            from fenris.generate.generator import GenerationResult

            return GenerationResult(
                state="INSUFFICIENT", raw_text="",
                error=f"local generation failed: {type(e).__name__}: {e}")

    def generate_stream(self, prompt: str):
        """Yield text chunks, then the parsed GenerationResult (the contract)."""
        from fenris.generate.generator import parse_structured

        self.calls.append(prompt)
        self._ensure_model()
        chunks: list[str] = []
        try:
            stream = self._llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.2,
                stream=True,
            )
            for piece in stream:
                delta = piece["choices"][0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    chunks.append(text)
                    yield text
        except LocalModelError:
            raise
        except Exception as e:  # noqa: BLE001
            from fenris.generate.generator import GenerationResult

            yield GenerationResult(
                state="INSUFFICIENT", raw_text="".join(chunks),
                error=f"local stream failed: {type(e).__name__}: {e}")
            return
        yield parse_structured("".join(chunks))


# ---- availability probe (for the doctor + provider switch) --------------------

def local_available() -> dict:
    """Which parts of the local stack are usable right now. Never raises."""
    out: dict = {"embedder": False, "generator": False, "detail": ""}
    try:
        import fastembed  # noqa: F401

        out["embedder"] = True
    except ImportError:
        out["detail"] += "pip install fastembed; "
    try:
        import llama_cpp  # noqa: F401

        out["generator"] = LocalGenerator.resolve_model_path() is not None
        if not out["generator"]:
            out["detail"] += f"set {LOCAL_GENERATE_MODEL_ENV}; "
    except ImportError:
        out["detail"] += "pip install llama-cpp-python; "
    return out
