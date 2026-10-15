"""Resolving a reference that may be a scoped name.

Many names are unique only inside a scope — a workload per project, a deployment or a
pool per cluster, a project per tenant, an artifact not at all. A bare name can then
match several rows, and picking the first or oldest of them is how a PATCH or a DELETE
lands on somebody else's object. Every lookup by name reads at most two rows and hands
them here, so "one", "none" and "more than one" are three different answers.
"""

from collections.abc import Sequence

from app.core.exceptions import Invalid


def at_most_one[T](found: Sequence[T], kind: str, ref: str, hint: str = "use its id") -> T | None:
    """The one row a reference names, None when it names none, and a 400 when it names several."""
    if len(found) > 1:
        raise Invalid(f"{ref!r} names more than one {kind}; {hint}", {"ref": ref, "kind": kind})
    return found[0] if found else None
