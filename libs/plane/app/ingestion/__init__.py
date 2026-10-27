"""Ingestion: writes arriving from the hub.

High frequency, idempotent, last-writer-wins per cluster. Consumes observed
state off hub sessions and denormalises it into Postgres so that the API and
placement never fan out across clusters.

Two rules this package exists to enforce:

- It writes observed state only. Declared state belongs to `app.modules`.
- Telemetry does not come through here. Utilisation, temperature, throughput and
  token counts go from the cluster to the observability stack directly.
"""

from app.ingestion.sink import DatabaseSink
from app.ingestion.sink import UnknownCluster
from app.ingestion.sink import DatabaseDesiredSource


__all__ = ["DatabaseDesiredSource", "DatabaseSink", "UnknownCluster"]
