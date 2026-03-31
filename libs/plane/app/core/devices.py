"""The canonical device-class dictionary, read from `contract/devices.json`.

A card is named in at least two dialects that will never agree — GPU Feature
Discovery publishes `NVIDIA-H100-80GB-HBM3` on a node label and the DCGM
exporter publishes `NVIDIA H100 80GB HBM3` on a Prometheus label — and neither
is ours. So the naming is: one table in `contract/`, read by this module and by
the operator's `internal/devices`, with the same normalization on both sides.

Nothing here is required of a cluster. Hardware the table does not list falls
back to the slug of the vendor string, which is stable and deterministic, and
the miss is logged once so the table can be grown.
"""

import json
import logging

from typing import Final
from pathlib import Path
from dataclasses import dataclass


logger = logging.getLogger(__name__)

DIALECT_GFD: Final = "gfd"
DIALECT_DCGM: Final = "dcgm"

TABLE_PATH: Final = Path(__file__).resolve().parents[4] / "contract" / "devices.json"


@dataclass(frozen=True)
class DeviceClass:
    """One card variant.

    Memory and form factor are part of the identity: `h100-80gb-sxm` and
    `h100-80gb-pcie` are different things to place against.
    """

    name: str
    vendor: str
    memory_mib: int
    form_factor: str
    aliases: dict[str, tuple[str, ...]]


def normalize(value: str) -> str:
    """The matching form of a vendor string.

    Lowercase, vendor prefix dropped, every run of non-alphanumeric characters
    collapsed to one hyphen. Identical to `devices.Normalize` in the operator,
    and tested as such — spaces against hyphens is the whole difference between
    the two dialects.
    """
    out = "".join(c if c.isalnum() else "-" for c in value.lower())
    while "--" in out:
        out = out.replace("--", "-")
    out = out.strip("-")
    return out.removeprefix("nvidia-")


slug = normalize
"""The fallback class for hardware the table does not name."""


class DeviceTable:
    """The dictionary, indexed by dialect and normalized alias."""

    def __init__(self, classes: list[DeviceClass]) -> None:
        self.classes = classes
        self._by_alias: dict[str, dict[str, DeviceClass]] = {}
        self._by_name = {c.name: c for c in classes}
        self._warned: set[tuple[str, str]] = set()
        for entry in classes:
            for dialect, aliases in entry.aliases.items():
                index = self._by_alias.setdefault(dialect, {})
                for alias in aliases:
                    index[normalize(alias)] = entry

    def lookup(self, dialect: str, product: str) -> DeviceClass | None:
        """Finds the class a vendor string names in one dialect."""
        if not product:
            return None
        return self._by_alias.get(dialect, {}).get(normalize(product))

    def resolve(self, dialect: str, product: str) -> str:
        """Maps a vendor string onto a hangar class, slugifying what it cannot."""
        if not product:
            return ""
        found = self.lookup(dialect, product)
        if found is not None:
            return found.name
        self._warn(dialect, product)
        return slug(product)

    def get(self, name: str) -> DeviceClass | None:
        return self._by_name.get(name)

    def aliases(self, name: str, dialect: str) -> tuple[str, ...]:
        """The vendor strings a hangar class is emitted under in one dialect."""
        entry = self._by_name.get(name)
        if entry is None:
            return ()
        return entry.aliases.get(dialect, ())

    def _warn(self, dialect: str, product: str) -> None:
        key = (dialect, product)
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning("devices.unmapped dialect=%s product=%r class=%s", dialect, product, slug(product))


def load(path: Path | None = None) -> DeviceTable:
    """Reads the contract table. A missing or broken file is not fatal."""
    source = path or TABLE_PATH
    try:
        document = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("devices.table_unreadable path=%s error=%s", source, exc)
        return DeviceTable([])
    classes = [
        DeviceClass(
            name=entry["class"],
            vendor=entry["vendor"],
            memory_mib=int(entry["memory_mib"]),
            form_factor=entry["form_factor"],
            aliases={k: tuple(v) for k, v in entry.get("aliases", {}).items()},
        )
        for entry in document.get("classes", [])
    ]
    return DeviceTable(classes)


_table: DeviceTable | None = None


def table() -> DeviceTable:
    """The process-wide table, parsed once."""
    global _table
    if _table is None:
        _table = load()
    return _table
