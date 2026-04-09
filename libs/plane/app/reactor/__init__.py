"""The reactor: derives alerts from declared-versus-observed state and from the event log.

Machinery, not a module. It owns no object in the model — `app/modules/alerts`
and `app/modules/notifications` are the API over what this writes — and everything
here would be just as true of a control plane managing something other than GPUs.

Sits alongside `app/ingestion` in the layering: ingestion writes observed state,
this reads it back against declared state and writes the one derived thing the
plane produces. It never writes intent and never talks to a cluster.
"""

from .loop import tick
from .loop import reactor_loop


__all__ = ["reactor_loop", "tick"]
