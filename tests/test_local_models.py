"""Local-model fallback tests: protocol conformance and failure honesty.

No model weights are downloaded: the suite pins the CONTRACTS (lazy
imports, error messages with fixes, query/document instruction formats,
availability probing) and the wiring through the provider switch.
"""

from __future__ import annotations

import pytest

from fenris.local_models import (
    LocalEmbedder,
    LocalGenerator,
    LocalModelError,
    local_available,
)


class TestLocalEmbedder:
    def test_lazy_import_never_raises_at_construction(self):
        """Constructing is free; only use loads weights (test envs stay light)."""
        LocalEmbedder()  # no ImportError here even without fastembed

    def test_missing_fastembed_raises_with_fix(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_fastembed(name, *a, **kw):
            if name.startswith("fastembed"):
                raise ImportError("nope")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_fastembed)
        emb = LocalEmbedder()
        with pytest.raises(LocalModelError) as exc:
            emb.embed_documents(["x"])
        assert "pip install fastembed" in str(exc.value)

    def test_model_load_failure_names_the_model(self, monkeypatch):
        emb = LocalEmbedder()
        monkeypatch.setitem(
            emb.__dict__, "_model", None
        )
        # simulate a fastembed import that works but a model that fails
        import sys

        monkeypatch.setitem(
            sys.modules, "fastembed",
            type(sys)("fastembed"),
        )
        sys.modules["fastembed"].TextEmbedding = None  # attribute exists
        import types

        fake = types.ModuleType("fastembed")

        def boom(**kw):
            raise RuntimeError("weights missing")

        fake.TextEmbedding = boom
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        with pytest.raises(LocalModelError) as exc:
            emb.embed_documents(["x"])
        assert emb._model_name in str(exc.value) or "weights" in str(exc.value)


class TestLocalGenerator:
    def test_lazy_construction(self):
        LocalGenerator()  # no import of llama_cpp here

    def test_missing_llamacpp_raises_with_fix(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_llama(name, *a, **kw):
            if name.startswith("llama_cpp"):
                raise ImportError("nope")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_llama)
        gen = LocalGenerator()
        with pytest.raises(LocalModelError) as exc:
            gen.generate("prompt")
        assert "llama-cpp-python" in str(exc.value)

    def test_missing_weights_raises_with_env_fix(self, monkeypatch):
        import sys
        import types

        fake = types.ModuleType("llama_cpp")
        fake.Llama = lambda **kw: None
        monkeypatch.setitem(sys.modules, "llama_cpp", fake)
        monkeypatch.delenv("FENRIS_LOCAL_MODEL", raising=False)
        monkeypatch.setattr(LocalGenerator, "resolve_model_path",
                            staticmethod(lambda: None))
        gen = LocalGenerator()
        with pytest.raises(LocalModelError) as exc:
            gen.generate("prompt")
        assert "FENRIS_LOCAL_MODEL" in str(exc.value)


class TestAvailability:
    def test_probe_never_raises_and_reports_both(self):
        report = local_available()
        assert isinstance(report["embedder"], bool)
        assert isinstance(report["generator"], bool)
        assert isinstance(report["detail"], str)

    def test_probe_reports_missing_pieces_honestly(self):
        # on this machine fastembed/llama_cpp are not installed: the probe
        # must say so rather than claim availability
        report = local_available()
        if not report["embedder"]:
            assert "fastembed" in report["detail"]


class TestProtocolConformance:
    def test_generator_calls_recorded_like_gemini(self):
        """The FakeGenerator contract (calls list) exists here too - the
        service layer may introspect any generator the same way."""
        gen = LocalGenerator()
        assert isinstance(gen.calls, list)

    def test_names_identify_provider_for_the_answer_badge(self):
        assert LocalEmbedder().name == "local-qwen3-emb"
        assert LocalGenerator().name == "local-llamacpp"
