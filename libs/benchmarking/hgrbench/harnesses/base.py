"""What a harness is to this runner, and the docker plumbing all of them share.

A harness here is three things: an image, a function that turns one cell into a
`docker run` argv, and a function that reads back what the tool wrote. Nothing
else — no metric knowledge (that is `metrics`), no scheduling (that is `runner`),
no opinion about where results go (that is `store`).

Two rules hold across every harness, and both come from being burned:

**The image is pulled by digest for the host architecture, never by tag.** AIPerf's
tag is multi-arch and its amd64 variant, under emulation on Apple silicon, starts
its supervisor process and then never spawns a worker — no output, no CPU, no
exit, forever. Docker will not swap variants for a tag it already holds, so a tag
alone cannot express "the one that runs here".

**The bearer token is passed as a container environment variable and expanded by a
shell inside the container.** Put it in the argv and it is in `ps` and in shell
history on the host. The one harness that has no header flag at all (GuideLLM)
still takes it this way, because its `api_key=` attribute is expanded by the
in-container shell rather than by ours.
"""

from __future__ import annotations

import json
import shlex
import subprocess

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Callable

from hgrbench.plan import Cell


TOKEN_ENV = "HGR_BENCH_TOKEN"
REDACTED = "$" + TOKEN_ENV


@dataclass(frozen=True)
class Context:
    """Everything a harness needs that is not the cell itself."""

    endpoint_url: str
    token: str
    hf_cache: Path
    results_dir: Path
    image: str
    platform: str = ""

    @property
    def has_token(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class Invocation:
    """One container run, and how to show it to a human without leaking the token."""

    argv: list[str]
    display: str


@dataclass
class Emission:
    """One measurement a harness produced, as the harness wrote it.

    A harness usually emits one of these per invocation. GuideLLM emits one per
    load point in a multi-point run, which is why this is a list everywhere.
    """

    summary: Any
    version: str
    load_label: str = ""
    load: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Harness:
    """A load generator this runner knows how to drive and read."""

    name: str
    image: str
    pinned_version: str
    command: Callable[[Cell, Context], Invocation]
    read: Callable[[Cell, Context], list[Emission]]
    description: str
    build_context: str = ""
    notes: tuple[str, ...] = ()
    sweep_command: Callable[[Context, Any], Invocation] | None = None
    sweep_read: Callable[[Context, Any], list[Emission]] | None = None

    @property
    def sweeps(self) -> bool:
        """Whether this harness can walk a whole ladder in one process."""
        return self.sweep_command is not None and self.sweep_read is not None


class HarnessError(RuntimeError):
    """A harness could not be prepared or its output could not be read."""


def host_arch() -> str:
    """`amd64` or `arm64`, as docker names them."""
    machine = subprocess.run(["uname", "-m"], capture_output=True, text=True, check=True).stdout.strip()
    return "amd64" if machine in {"x86_64", "amd64"} else "arm64"


def resolve_digest(image: str, arch: str) -> str:
    """The image reference pinned to the digest of this architecture's variant.

    Returns the image unchanged when it is not in a registry we can query — a
    locally built image has no manifest list and needs no pinning, because it was
    built here and is already the right architecture.
    """
    probe = subprocess.run(["docker", "manifest", "inspect", image], capture_output=True, text=True)
    if probe.returncode != 0:
        return image
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return image
    manifests = payload.get("manifests")
    if not manifests:
        return image
    for entry in manifests:
        platform = entry.get("platform", {})
        if platform.get("architecture") == arch and platform.get("os") == "linux":
            repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            return f"{repo}@{entry['digest']}"
    raise HarnessError(f"{image} has no linux/{arch} variant")


def image_exists(image: str) -> bool:
    """Whether the image is already in the local daemon."""
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    return probe.returncode == 0


def pull(image: str) -> None:
    """Pull an image, quietly, failing loudly."""
    done = subprocess.run(["docker", "pull", "-q", image], capture_output=True, text=True)
    if done.returncode != 0:
        raise HarnessError(f"docker pull {image} failed: {done.stderr.strip()}")


def docker_argv(
    ctx: Context,
    *,
    mount_results_at: str,
    env: dict[str, str] | None = None,
    entrypoint: str = "",
    args: list[str] | None = None,
    mount_hf_at: str = "/hf",
) -> list[str]:
    """A `docker run` argv with the mounts and environment every harness wants.

    The Hugging Face cache is mounted writable rather than read-only: every one of
    these harnesses needs a tokenizer, and `huggingface_hub` treats a cache it
    cannot write as no cache at all — asked for a repository it does not hold, it
    creates the directory before it checks anything and dies on a read-only mount
    rather than on a missing file.
    """
    argv = ["docker", "run", "--rm"]
    if ctx.platform:
        argv += ["--platform", ctx.platform]
    argv += ["-e", f"HF_HOME={mount_hf_at}"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [
        "-v",
        f"{ctx.hf_cache}:{mount_hf_at}",
        "-v",
        f"{ctx.results_dir}:{mount_results_at}",
    ]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    argv.append(ctx.image)
    argv += args or []
    return argv


def redacted(argv: list[str], token: str) -> str:
    """The command as a human should read it, with the token taken out.

    The token is replaced rather than removed so that the printed command is still
    the shape of the one that ran, and so that a reader can see *where* the
    credential went.
    """
    shown = []
    for part in argv:
        if token and token in part:
            part = part.replace(token, REDACTED)
        shown.append(part)
    return " ".join(shlex.quote(part) for part in shown)


def with_token(ctx: Context, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a run, with the token under its one agreed name."""
    out = dict(env or {})
    if ctx.has_token:
        out[TOKEN_ENV] = ctx.token
    return out


def shell_command(ctx: Context, script: str, *, mount_results_at: str, entrypoint: str = "sh") -> Invocation:
    """A harness driven through a shell inside the container.

    The script is a single argument, unexpanded by our shell, so `$HGR_BENCH_TOKEN`
    in it is expanded by the container's shell from the environment — which is the
    whole point.
    """
    argv = docker_argv(
        ctx,
        mount_results_at=mount_results_at,
        env=with_token(ctx),
        entrypoint=entrypoint,
        args=["-c", script],
    )
    return Invocation(argv, redacted(argv, ctx.token))


def read_json(path: Path) -> Any:
    """One JSON file, or a clear complaint about why not."""
    if not path.exists():
        raise HarnessError(f"{path.name} was not written")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{path.name} is not valid JSON: {exc}") from exc


def find_one(root: Path, pattern: str) -> Path:
    """The single file matching a glob, or a complaint naming what was there."""
    matches = sorted(root.rglob(pattern))
    if not matches:
        listing = ", ".join(sorted(child.name for child in root.iterdir())) or "nothing"
        raise HarnessError(f"no {pattern} under {root.name}; found {listing}")
    return matches[0]
