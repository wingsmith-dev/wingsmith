"""What a harness is to this runner, and the docker plumbing all of them share.

A harness here is three things: an image, a function that turns one cell into a
`docker run` argv, and a function that reads back what the tool wrote. Nothing
else: no metric knowledge (that is `metrics`), no scheduling (that is `runner`),
no opinion about where results go (that is `store`).

Two rules hold across every harness, and both come from being burned:

**The image is pulled by digest for the host architecture, never by tag.** AIPerf's
tag is multi-arch and its amd64 variant, under emulation on Apple silicon, starts
its supervisor process and then never spawns a worker: no output, no CPU, no
exit, forever. Docker will not swap variants for a tag it already holds, so a tag
alone cannot express "the one that runs here".

**The bearer token is passed as a container environment variable and expanded by a
shell inside the container.** The `docker run` argv names the variable and never
carries its value (`-e HGR_BENCH_TOKEN`, inherited from the docker CLI's own
environment), so the docker CLI's command line, the commands this runner records
and its logs never hold the token. The one harness that has no header flag at all
(GuideLLM) still takes it this way, because its `api_key=` attribute is expanded by
the in-container shell, not by ours.

That is as far as it goes. The in-container shell expands the variable into the
argv of the tool it starts, AIPerf's `-H`, GuideLLM's `--backend`, `vllm bench
serve`'s `--header` and the vLLM sweep's `--bench-cmd`, because none of them takes
the token from its environment for every request it sends.
On a Linux host, where container processes share the host's process table, those
command lines are visible to anyone who can run `ps` there. Use a token scoped to
the endpoint under test, on a host you trust.
"""

from __future__ import annotations

import json
import uuid
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
# A token shorter than this is left in place: replacing a value like "x" everywhere it appears
# mangles paths and words in every printed command and log line, and a value that short is not a
# credential worth hiding.
MIN_REDACTED_LENGTH = 8


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
    env: dict[str, str] = field(default_factory=dict)
    """Set in the docker CLI's environment, for the `-e NAME` entries in `argv` to inherit."""
    container: str = ""
    """The `--name` the container runs under, so a run that overstays can be killed by it."""


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
    digest: str = ""
    """The digest of the multi-arch index `image` named when it was pinned.

    The tag is kept for people to read; the digest is what runs. A tag can be moved
    to a different build, and a run that cannot say which build it measured cannot
    be reproduced.
    """
    build_context: str = ""
    notes: tuple[str, ...] = ()
    sweep_command: Callable[[Context, Any], Invocation] | None = None
    sweep_read: Callable[[Context, Any], list[Emission]] | None = None

    @property
    def reference(self) -> str:
        """What to resolve and pull: the pinned index when there is one, else the image."""
        return f"{repository(self.image)}@{self.digest}" if self.digest else self.image

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


def repository(image: str) -> str:
    """An image reference without its tag or digest.

    The tag separator is the last `:` in the last path segment, so a registry port
    (`localhost:5000/name`) is not mistaken for one.
    """
    name = image.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    if ":" in last:
        last = last.rsplit(":", 1)[0]
    return f"{head}/{last}" if head else last


def resolve_digest(image: str, arch: str) -> str:
    """The image reference pinned to the digest of this architecture's variant.

    Fails instead of falling back to the tag. A registry that cannot be queried, an
    answer that is not a manifest list, or a list without this architecture all mean
    the run would measure an image nobody can name afterwards, and on Apple silicon a
    tag can quietly resolve to an amd64 variant that hangs under emulation. Images
    built locally never come here: they were built for this machine and have no
    registry to ask.
    """
    probe = subprocess.run(["docker", "manifest", "inspect", image], capture_output=True, text=True)
    if probe.returncode != 0:
        detail = probe.stderr.strip() or f"exit {probe.returncode}"
        raise HarnessError(f"could not read the manifest of {image} to pin it by digest: {detail}")
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"the manifest of {image} is not JSON: {exc}") from exc
    manifests = payload.get("manifests") if isinstance(payload, dict) else None
    if not manifests:
        if "@sha256:" in image:
            return image
        raise HarnessError(f"{image} is a single-architecture image named by tag; pin it by digest instead")
    for entry in manifests:
        platform = entry.get("platform", {})
        if platform.get("architecture") == arch and platform.get("os") == "linux":
            return f"{repository(image)}@{entry['digest']}"
    raise HarnessError(f"{image} has no linux/{arch} variant")


def recorded_version(found: object, harness: Harness) -> tuple[str, list[str]]:
    """The version an export says produced it, or the pinned image's, said out loud.

    The field maps are chosen by version, so a missing one cannot be left blank. When
    the export does not record it, the version is the one the pinned image digest was
    published as, and the measurement carries a note saying so instead of presenting
    an inferred version as a reported one.
    """
    if isinstance(found, str) and found.strip():
        return found.strip(), []
    return harness.pinned_version, [
        f"the export does not record its {harness.name} version; read as {harness.pinned_version}, "
        f"the version of the pinned image {harness.reference}"
    ]


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
    inherit: tuple[str, ...] = (),
    entrypoint: str = "",
    args: list[str] | None = None,
    mount_hf_at: str = "/hf",
    name: str = "",
) -> list[str]:
    """A `docker run` argv with the mounts and environment every harness wants.

    The Hugging Face cache is mounted writable, not read-only: every one of
    these harnesses needs a tokenizer, and `huggingface_hub` treats a cache it
    cannot write as no cache at all: asked for a repository it does not hold, it
    creates the directory before it checks anything and dies on a read-only mount,
    not on a missing file.
    """
    argv = ["docker", "run", "--rm"]
    if name:
        argv += ["--name", name]
    if ctx.platform:
        argv += ["--platform", ctx.platform]
    argv += ["-e", f"HF_HOME={mount_hf_at}"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    # Name only: docker copies the value from its own environment, so it is never on a command line.
    for key in inherit:
        argv += ["-e", key]
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


def scrub(text: str, token: str) -> str:
    """`text` with every occurrence of the token replaced by its variable name.

    Replacing it with the variable name keeps what is printed in the shape of what
    ran, and shows a reader where the credential went.
    """
    if len(token) < MIN_REDACTED_LENGTH:
        return text
    return text.replace(token, REDACTED)


def redacted(argv: list[str], token: str) -> str:
    """The command as a human should read it, with the token taken out.

    The argv carries the token only by its variable name today, so this is a guard
    against a harness that starts passing it inline.
    """
    return " ".join(shlex.quote(scrub(part, token)) for part in argv)


def quote(value: object) -> str:
    """One value for the in-container shell, as a single word it will not expand.

    Every value interpolated into a harness script goes through this, so a model
    name, tokenizer or URL with a space, a quote or a `$` in it reaches the tool as
    written. The token is the one thing left for that shell to expand,
    and it is never passed through here.
    """
    return shlex.quote(str(value))


def double_quoted(text: str) -> str:
    """`text` inside double quotes, with only `$TOKEN_ENV` left for the shell to expand.

    For the one place a script has to hand a whole command line to a tool that
    splits it again itself: the outer shell must expand the token, and must not
    expand or split anything else.
    """
    marker = "\x00token\x00"
    held = text.replace(f"${{{TOKEN_ENV}}}", marker).replace(f"${TOKEN_ENV}", marker)
    for char in ("\\", '"', "$", "`"):
        held = held.replace(char, "\\" + char)
    return '"' + held.replace(marker, f"${{{TOKEN_ENV}}}") + '"'


def with_token(ctx: Context) -> dict[str, str]:
    """The docker CLI's extra environment for a run: the token under its one agreed name."""
    return {TOKEN_ENV: ctx.token} if ctx.has_token else {}


def shell_command(ctx: Context, script: str, *, mount_results_at: str, entrypoint: str = "sh") -> Invocation:
    """A harness driven through a shell inside the container.

    The script is a single argument, unexpanded by our shell, so `$HGR_BENCH_TOKEN`
    in it is expanded by the container's shell from the environment, which is the
    whole point.
    """
    secrets = with_token(ctx)
    name = container_name()
    argv = docker_argv(
        ctx,
        mount_results_at=mount_results_at,
        inherit=tuple(secrets),
        entrypoint=entrypoint,
        args=["-c", script],
        name=name,
    )
    return Invocation(argv, redacted(argv, ctx.token), env=secrets, container=name)


def container_name() -> str:
    """A name no other run on this daemon is using."""
    return f"hgrbench-{uuid.uuid4().hex[:12]}"


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
