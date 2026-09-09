"""The answer layer (the build plan).

Public surface - the CLI integrator wires these:

-:func:`fenris.generate.ask` - the answer orchestrator entry point
 ``ask(conn, query, qvec, *, generator, search_fn,...) -> AnswerPacket``
-:func:`fenris.generate.ask_stream` - the streaming variant for the CLI
-:class:`fenris.generate.AnswerPacket` - the final product
-:class:`fenris.generate.Generator` (protocol) /:class:`fenris.generate.GeminiGenerator`
 /:class:`fenris.generate.FakeGenerator` /:class:`fenris.generate.GenerationResult`
-:class:`fenris.generate.Toolbox` /:func:`fenris.generate.run_agentic_loop`
 (the bounded retry loop; ``ask`` drives it, it is also usable directly)
-:class:`fenris.generate.VerificationResult` /:func:`fenris.generate.verify_sources`
 /:func:`fenris.generate.resolve_state` /:func:`fenris.generate.conflict_precheck`
-:func:`fenris.generate.render_answer` /:class:`fenris.generate.RenderedAnswer`
 /:class:`fenris.generate.Citation`
-:func:`fenris.generate.u_shape` /:func:`fenris.generate.assemble_prompt`
 /:func:`fenris.generate.build_messages` /:func:`fenris.generate.system_prompt`

Injection seams (see service.py docstring): ``search_fn`` (the CLI partially
applies ``fenris.retrieve.search`` with conn/qvec/reranker), ``refresh_fn``
(the STALE drain-and-retry; defaults to re-running search_fn until the sync layer wires
the real Drive drain), and ``generator`` (GeminiGenerator in production,
FakeGenerator in tests).
"""

from fenris.generate.agent import (
    AgenticResult,
    Toolbox,
    ToolCall,
    run_agentic_loop,
    trace_from,
)
from fenris.generate.generator import (
    FakeGenerator,
    GeminiGenerator,
    GenerationResult,
    Generator,
    parse_structured,
)
from fenris.generate.prompts import (
    assemble_prompt,
    build_messages,
    system_prompt,
    u_shape,
)
from fenris.generate.render import Citation, RenderedAnswer, render_answer
from fenris.generate.service import AnswerPacket, ask, ask_stream
from fenris.generate.verify import (
    VerificationResult,
    conflict_precheck,
    resolve_state,
    verify_sources,
)

__all__ = [
    "AgenticResult",
    "AnswerPacket",
    "Citation",
    "FakeGenerator",
    "GeminiGenerator",
    "GenerationResult",
    "Generator",
    "RenderedAnswer",
    "ToolCall",
    "Toolbox",
    "VerificationResult",
    "ask",
    "ask_stream",
    "assemble_prompt",
    "build_messages",
    "conflict_precheck",
    "parse_structured",
    "render_answer",
    "resolve_state",
    "run_agentic_loop",
    "system_prompt",
    "trace_from",
    "u_shape",
    "verify_sources",
]
