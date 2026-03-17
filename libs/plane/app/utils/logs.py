import sys
import logging

from app.config import settings


_FORMAT_TEXT = "%(asctime)s %(levelname)-8s %(name)s [%(process_name)s] %(message)s"
_FORMAT_JSON = '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","process":"%(process_name)s","msg":"%(message)s"}'


class _ProcessFilter(logging.Filter):
    """Stamps every record with the name of the plane process that emitted it.

    The API and the hub are separate processes writing to one log pipeline, so a
    record without this field cannot be attributed.
    """

    def __init__(self, process: str) -> None:
        super().__init__()
        self.process = process

    def filter(self, record: logging.LogRecord) -> bool:
        record.process_name = self.process
        return True


def configure(process: str) -> None:
    """Installs the shared logging configuration for a plane process."""
    level = getattr(logging, settings.APP_LOG_LEVEL.upper(), logging.INFO)
    fmt = _FORMAT_JSON if settings.APP_LOG_FORMAT == "json" else _FORMAT_TEXT

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(_ProcessFilter(process))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "websockets", "sqlalchemy.engine"):
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
