"""Local benchmark harnesses, one comparable result shape, one report.

`hgrbench` runs the load generators this project cares about, AIPerf, vLLM's
`bench serve`, vLLM's own parameter sweep, and GuideLLM, against any
OpenAI-compatible endpoint, from a laptop, in containers, one at a time. It keeps
each tool's export verbatim and normalises through explicit per-harness field maps
whose ids are shared with the plane's own `benchmarks` module.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
