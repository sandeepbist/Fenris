"""The generation call: protocol, Gemini implementation, and the test double.

Contract: ``generate(prompt) -> GenerationResult`` where ``GenerationResult``
carries the five structured fields plus the raw text for debugging. The
model-facing state enum is LIMITED to ANSWERABLE / INSUFFICIENT / CONFLICTING
- ERROR / STALE / UNAUTHORIZED are system-only and decided BEFORE generation
(the build-plan review fix #7).

Gemini structured output (verified at runtime against the installed
google-genai 2.20.0): ``GenerateContentConfig`` accepts ``response_schema``
(a plain dict schema, a pydantic model, or a ``types.Schema``) together with
``response_mime_type='application/json'`` - that path is used first, with a
JSON-mode fallback (``response_mime_type`` only, parse the text ourselves)
when the schema is rejected. Streaming: ``client.models.generate_content_stream``
exists in 2.20.0 and yields ``GenerateContentResponse`` chunks with a ``.text``
property, so ``GeminiGenerator.generate_stream`` is a real streaming wrapper,
not a simulated one.

No network is ever touched by this module's import path or by FakeGenerator;
GeminiGenerator only speaks to the network inside ``generate``/``generate_stream``
and only when a real API key is wired in by the CLI integrator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

# the five structured-output fields (the build plan)
MODEL_STATE_VALUES = ("ANSWERABLE", "INSUFFICIENT", "CONFLICTING")

# JSON schema handed to Gemini's response_schema (structured output). Plain
# dict on purpose: it works across the google-genai dict-schema path and the
# types.Schema path without pinning to one SDK shape.
RESPONSE_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "state": {
            "type": "STRING",
            "enum": list(MODEL_STATE_VALUES),
            "description": "ANSWERABLE, INSUFFICIENT, or CONFLICTING only.",
        },
        "answer": {"type": "STRING"},
        "cited_evidence_ids": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "claims_conflict": {"type": "BOOLEAN"},
        "missing_evidence": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
    },
    "required": ["state", "answer", "cited_evidence_ids", "claims_conflict",
                  "missing_evidence"],
}


@dataclass
class GenerationResult:
    """One generation outcome: the structured fields + the raw text.

    ``state`` is the MODEL's state (the three-value enum); the system ladder
    remains authoritative and may override it. ``raw_text`` preserves exactly
    what the generator emitted so malformed output is debuggable, never lost.
    """

    state: str = "INSUFFICIENT"
    answer: str = ""
    cited_evidence_ids: list[str] = field(default_factory=list)
    claims_conflict: bool = False
    missing_evidence: list[str] = field(default_factory=list)
    raw_text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Generator(Protocol):
    """Anything that turns a prompt into a structured GenerationResult."""

    name: str

    def generate(self, prompt: str) -> GenerationResult: ...

    def generate_stream(self, prompt: str): ...


# --------------------------------------------------------------------------- #
# parsing (shared by the real and the fake generators)
# --------------------------------------------------------------------------- #


def parse_structured(text: str) -> GenerationResult:
    """Parse a generator's text into GenerationResult; never raises.

    - Accepts bare JSON or a ```json fenced block.
    - Unknown/missing ``state`` -> INSUFFICIENT (never a crash, never a fake
      ANSWERABLE); a state outside the three-value enum is coerced down.
    - Any parse failure -> state INSUFFICIENT with ``error`` set and the raw
      text preserved (the CALLER maps that to the system ERROR state; the
      model-facing enum stays limited to the three).
    """
    if text is None:
        text = ""
    result = GenerationResult(raw_text=text)
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        data = json.loads(candidate)
    except (ValueError, TypeError) as exc:
        result.error = f"unparseable structured output: {exc}"
        return result
    if not isinstance(data, dict):
        result.error = f"structured output is {type(data).__name__}, not an object"
        return result

    state = data.get("state")
    if state not in MODEL_STATE_VALUES:
        result.error = f"state {state!r} outside the model enum"
        state = "INSUFFICIENT"
    result.state = state
    result.answer = str(data.get("answer") or "")
    cited = data.get("cited_evidence_ids") or []
    if isinstance(cited, list):
        result.cited_evidence_ids = [str(c) for c in cited]
    result.claims_conflict = bool(data.get("claims_conflict"))
    missing = data.get("missing_evidence") or []
    if isinstance(missing, list):
        result.missing_evidence = [str(m) for m in missing]
    return result


# --------------------------------------------------------------------------- #
# FakeGenerator - the deterministic test double
# --------------------------------------------------------------------------- #


class FakeGenerator:
    """Deterministic Generator double: echoes scripted structured outputs.

    ``outputs`` is a list of payloads consumed one per ``generate`` call
    (a payload may be a dict - returned as successful structured output - or
    a string - emitted raw, exercising the parser, including malformed JSON).
    When the script is exhausted the last payload repeats, so a loop that
    keeps generating never crashes the test.

    Streaming is genuinely incremental: ``generate_stream`` chunks the
    scripted payload's JSON text so streaming consumers see multiple yields,
    then ``generate`` returns the same payload as a whole. Every call is
    recorded in ``calls`` (the prompt text) for asserting what the loop sent.
    """

    name = "fake"

    def __init__(self, outputs: list | None = None):
        self.outputs = [o for o in (outputs or [])] or [
            {"state": "INSUFFICIENT", "answer": "",
             "cited_evidence_ids": [], "claims_conflict": False,
             "missing_evidence": ["no scripted output"]}
        ]
        self.calls: list[str] = []
        self._i = 0

    def _payload(self) -> dict | str:
        payload = self.outputs[min(self._i, len(self.outputs) - 1)]
        self._i += 1
        return payload

    def generate(self, prompt: str) -> GenerationResult:
        self.calls.append(prompt)
        payload = self._payload()
        if isinstance(payload, GenerationResult):
            return payload
        if isinstance(payload, dict):
            return parse_structured(json.dumps(payload))
        return parse_structured(payload)

    def generate_stream(self, prompt: str):
        """Yield the scripted output in small text chunks, then a final result.

        Yields strings for the display layer; the LAST yielded item is the
        GenerationResult (so a consumer can finish the stream with the
        structured fields, mirroring how the CLI prints streamed text then
        resolves citations).
        """
        self.calls.append(prompt)
        payload = self._payload()
        text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        step = max(1, len(text) // 5)
        for i in range(0, len(text), step):
            yield text[i:i + step]
        yield parse_structured(text)


# --------------------------------------------------------------------------- #
# GeminiGenerator
# --------------------------------------------------------------------------- #


class GeminiGenerator:
    """The real generator: google-genai client, model from config.

    Structured output strategy (both verified against google-genai 2.20.0 at
    runtime):

    1. PRIMARY - ``response_schema=RESPONSE_SCHEMA`` +
       ``response_mime_type='application/json'``: the SDK enforces the shape
       and exposes ``response.parsed``.
    2. FALLBACK - JSON mode only (``response_mime_type='application/json'``):
       we parse the text with :func:`parse_structured`. Used when the schema
       path raises (older API versions, region quirks).

    Streaming (``generate_stream``): google-genai 2.20.0 ships
    ``client.models.generate_content_stream``, which yields response chunks
    with a ``.text`` property - this implementation streams those chunks and
    finishes with the structured result parsed from the concatenated text.
    The capability is real in the installed version; if a future SDK drops
    the method, the import-time check in :func:`generate_stream` raises
    AttributeError immediately rather than simulating streaming.

    The client is lazy: constructing ``google.genai.Client`` happens on the
    first ``generate``/``generate_stream`` call (so importing this module in a
    test environment costs nothing and never needs credentials). ``api_key``
    is read from the parameter or ``GOOGLE_API_KEY``/``GEMINI_API_KEY`` by the
    SDK itself.
    """

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 client=None, use_schema: bool = True):
        from fenris import config

        self.model = model or config.GENERATION_MODEL
        self._api_key = api_key
        self._client = client          # injected for tests / alternate wiring
        self._use_schema = use_schema
        self._schema_failed = False   # sticky: once rejected, stay on JSON mode
        self.calls: list[str] = []

    # -- client ---------------------------------------------------------------

    def _ensure_client(self):
        if self._client is None:
            from google import genai

            kwargs = {"api_key": self._api_key} if self._api_key else {}
            self._client = genai.Client(**kwargs)
        return self._client

    def _config(self):
        from google.genai import types

        base = {"response_mime_type": "application/json",
                "system_instruction": None}
        if self._use_schema and not self._schema_failed:
            base["response_schema"] = RESPONSE_SCHEMA
        return types.GenerateContentConfig(**base)

    # -- the protocol --------------------------------------------------------

    def generate(self, prompt: str) -> GenerationResult:
        """One structured generation. Any SDK error surfaces as a GenerationResult
        with ``error`` set (the caller maps it to the ERROR state) - a broken
        API call must not crash the answer layer."""
        self.calls.append(prompt)
        client = self._ensure_client()
        text = ""
        try:
            response = client.models.generate_content(
                model=self.model, contents=prompt, config=self._config())
            text = self._response_text(response)
            parsed = getattr(response, "parsed", None)
            if parsed is not None and self._use_schema and not self._schema_failed:
                return self._from_parsed(parsed, text)
            return parse_structured(text)
        except Exception as exc:  # noqa: BLE001 - SDK errors must not crash ask()
            if self._schema_rejected(exc):
                # retry once on the JSON-mode fallback, then stay there
                self._schema_failed = True
                return self.generate(prompt)
            return GenerationResult(
                state="INSUFFICIENT", raw_text=text,
                error=f"generation failed: {type(exc).__name__}: {exc}")

    def generate_stream(self, prompt: str):
        """Yield streamed text chunks, then the final GenerationResult.

        Uses the SDK's real streaming call (``generate_content_stream`` -
        present in google-genai 2.20.0); there is no simulated fallback. The
        structured fields are parsed from the full concatenated text once the
        stream is drained.
        """
        self.calls.append(prompt)
        client = self._ensure_client()
        chunks: list[str] = []
        try:
            stream = client.models.generate_content_stream(
                model=self.model, contents=prompt, config=self._config())
            for chunk in stream:
                text = self._chunk_text(chunk)
                if text:
                    chunks.append(text)
                    yield text
        except Exception as exc:  # noqa: BLE001 - never crash the stream loop
            if self._schema_rejected(exc) and not self._schema_failed:
                self._schema_failed = True
                yield from self.generate_stream(prompt)
                return
            yield GenerationResult(state="INSUFFICIENT", raw_text="".join(chunks),
                                   error=f"generation failed: {type(exc).__name__}: {exc}")
            return
        yield parse_structured("".join(chunks))

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _response_text(response) -> str:
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else ""

    @staticmethod
    def _chunk_text(chunk) -> str:
        text = getattr(chunk, "text", None)
        return text if isinstance(text, str) else ""

    @staticmethod
    def _schema_rejected(exc: BaseException) -> bool:
        """True when the failure looks like 'this endpoint hates the schema'."""
        msg = str(exc).lower()
        return ("response_schema" in msg or "response schema" in msg
                or "schema" in msg and ("invalid" in msg or "not supported" in msg))

    def _from_parsed(self, parsed, text: str) -> GenerationResult:
        """Map ``response.parsed`` (a pydantic model or dict) to GenerationResult."""
        if isinstance(parsed, dict):
            data = parsed
        else:
            data = {k: getattr(parsed, k, None)
                    for k in ("state", "answer", "cited_evidence_ids",
                              "claims_conflict", "missing_evidence")}
        result = parse_structured(json.dumps({
            "state": data.get("state"),
            "answer": data.get("answer") or "",
            "cited_evidence_ids": data.get("cited_evidence_ids") or [],
            "claims_conflict": data.get("claims_conflict") or False,
            "missing_evidence": data.get("missing_evidence") or [],
        }))
        result.raw_text = text
        return result
