"""What this runner needs to know about one endpoint, and nothing a probe should know.

Every piece of vendor knowledge in this package lives here. A probe that needs to
know how a particular vendor spells thinking mode, or where it puts the cached
token count, reads it from the profile — it never branches on a vendor name. That
is the whole reason a probe written for one model keeps working for the next one.

A profile is committed TOML for the same reason a benchmark plan is: what got
checked, and against what, should be reviewable in a diff rather than recoverable
from somebody's shell history.
"""

from __future__ import annotations

import os
import tomllib

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass


# A Hangar tenant API key, when a profile names no variable of its own.
DEFAULT_TOKEN_ENV = "HANGAR_API_KEY"

THINKING_SYNTAXES = ("none", "kimi", "chat_template", "reasoning_effort", "system_prompt")


@dataclass(frozen=True)
class Thinking:
    """How this endpoint is told to think, if it can.

    Four shapes exist in the wild and none of them is in the OpenAI spec, so the
    probes cannot guess. `kimi` is `extra_body.thinking.type`, `chat_template` is
    the open-source frameworks' `chat_template_kwargs.thinking`, `reasoning_effort`
    is the top-level parameter OpenAI-compatible servers added for reasoning models,
    and `system_prompt` is a line of text prepended to the conversation.

    The last one is not a request field at all, which is why `body()` cannot express
    it and `preamble()` exists beside it. gpt-oss is the case: vLLM's own recipe sets
    reasoning effort with `{"role": "system", "content": "Reasoning: high"}` and
    accepts no parameter for it, so a probe that sends `reasoning_effort` gets a 200
    and changes nothing — the quietest way to record an endpoint as conforming to a
    contract it never saw.
    """

    syntax: str = "none"
    default_on: bool | None = None
    efforts: tuple[str, ...] = ()
    prompt_template: str = "Reasoning: {effort}"

    def preamble(self, effort: str = "") -> list[dict[str, str]]:
        """The messages to prepend, for an endpoint that carries thinking in the prompt."""
        if self.syntax != "system_prompt" or not effort:
            return []
        return [{"role": "system", "content": self.prompt_template.format(effort=effort)}]

    def body(self, enabled: bool, effort: str = "") -> dict[str, Any]:
        """The request fields that turn thinking on or off in this endpoint's dialect.

        Empty for `system_prompt`, where the switch is a message and not a field. A
        probe must send `preamble()` as well to cover every syntax.
        """
        if self.syntax == "system_prompt":
            return {}
        if self.syntax == "none":
            return {}
        if self.syntax == "kimi":
            thinking: dict[str, Any] = {"type": "enabled" if enabled else "disabled"}
            if enabled and effort:
                thinking |= {"keep": "all", "effort": effort}
            return {"thinking": thinking}
        if self.syntax == "chat_template":
            kwargs: dict[str, Any] = {"thinking": enabled}
            if enabled and effort:
                kwargs |= {"preserve_thinking": True, "thinking_effort": effort}
            return {"chat_template_kwargs": kwargs}
        if self.syntax == "reasoning_effort":
            return {"reasoning_effort": effort or ("low" if enabled else "none")}
        raise ValueError(f"unknown thinking syntax {self.syntax!r}")


@dataclass(frozen=True)
class Paths:
    """Where this endpoint puts the values that have no agreed location.

    Dotted paths into the response body. The pattern, and the reason for it, is the
    same as the per-harness field maps in `hgrbench.metrics`: a value read through a
    declared path is traceable, and a path that does not resolve is a named miss
    rather than a silent zero.
    """

    cached_tokens: str = "usage.prompt_tokens_details.cached_tokens"
    reasoning: str = "choices.0.message.reasoning_content"
    reasoning_delta: str = "choices.0.delta.reasoning_content"


@dataclass(frozen=True)
class Profile:
    """One endpoint under test, and what it claims about itself.

    The claims matter as much as the endpoint. `context_window` and `modalities` are
    what a probe checks the endpoint against when there is no reference endpoint to
    diff it with; with a reference, they are only a cross-check.

    `extra_body` is merged into every request this endpoint is sent, under whatever
    the probe set itself. It exists for the fields that select *which server* answers
    rather than what is asked of it, and a broker is the case: a broker can serve one
    model id from many upstreams and pick per request, so an unpinned reference is
    not the model's original provider but whichever re-host it chose that second.
    That is vendor knowledge of the purest kind, which is why it is a profile field
    and not something a probe could ever be allowed to know.

    `routes` is the same idea for the URL. A probe names a surface by its canonical
    path — `/v1/messages` is the Anthropic surface, everywhere, by definition — and
    the profile says where this vendor happens to mount it. A provider that serves
    it from `/anthropic/v1/messages` answers 404 at the canonical path; without the
    rewrite the run records "this endpoint has no Anthropic surface", which is both
    false and exactly the kind of finding that survives review because it looks
    like data.
    """

    name: str
    base_url: str
    model: str
    token_env: str = DEFAULT_TOKEN_ENV
    context_window: int = 0
    modalities: tuple[str, ...] = ("text",)
    protocols: tuple[str, ...] = ("chat_completions",)
    leak_needles: tuple[str, ...] = ()
    timeout: float = 120.0
    extra_body: dict[str, Any] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    thinking: Thinking = field(default_factory=Thinking)
    paths: Paths = field(default_factory=Paths)
    source: Path | None = None

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")

    def supports(self, capability: str) -> bool:
        return capability in self.modalities or capability in self.protocols


def load_profile(path: Path) -> Profile:
    """Read a profile from TOML, refusing the mistakes that only fail much later."""
    raw = tomllib.loads(path.read_text())

    thinking_raw = raw.get("thinking", {})
    syntax = thinking_raw.get("syntax", "none")
    if syntax not in THINKING_SYNTAXES:
        raise ValueError(f"{path}: thinking.syntax {syntax!r} is not one of {', '.join(THINKING_SYNTAXES)}")

    for required in ("name", "base_url", "model"):
        if not raw.get(required):
            raise ValueError(f"{path}: {required} is required")

    return Profile(
        name=raw["name"],
        base_url=raw["base_url"].rstrip("/"),
        model=raw["model"],
        token_env=raw.get("token_env", DEFAULT_TOKEN_ENV),
        context_window=int(raw.get("context_window", 0)),
        modalities=tuple(raw.get("modalities", ["text"])),
        protocols=tuple(raw.get("protocols", ["chat_completions"])),
        leak_needles=tuple(raw.get("leak_needles", [])),
        timeout=float(raw.get("timeout", 120.0)),
        extra_body=raw.get("extra_body", {}),
        routes=raw.get("routes", {}),
        thinking=Thinking(
            syntax=syntax,
            default_on=thinking_raw.get("default_on"),
            efforts=tuple(thinking_raw.get("efforts", [])),
            prompt_template=thinking_raw.get("prompt_template", "Reasoning: {effort}"),
        ),
        paths=Paths(**raw.get("paths", {})),
        source=path,
    )
