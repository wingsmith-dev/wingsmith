"""The canonical device-class dictionary, read from `libs/packages/contract/devices.json`.

A card is named in at least two dialects that will never agree — GPU Feature
Discovery publishes `NVIDIA-H100-80GB-HBM3` on a node label and the DCGM
exporter publishes `NVIDIA H100 80GB HBM3` on a Prometheus label — and neither
is ours. So the naming is: one table in `libs/packages/contract/`, read by this module and by
the operator's `internal/devices`, with the same normalization on both sides.

Nothing here is required of a cluster. Hardware the table does not list falls
back to the slug of the vendor string, which is stable and deterministic, and
the miss is logged once so the table can be grown.
"""

import os
import json
import logging

from typing import Final
from pathlib import Path
from dataclasses import dataclass


logger = logging.getLogger(__name__)

DIALECT_GFD: Final = "gfd"
DIALECT_DCGM: Final = "dcgm"

DEVICE_TABLE_ENV: Final = "HANGAR_DEVICE_TABLE"


def _table_path() -> Path:
    """Finds `libs/packages/contract/devices.json` in a checkout and in the image alike.

    The table is shared with the operator, so it lives in `libs/packages/contract/` at the repo
    root rather than inside this package — four levels above this module in a
    checkout, but two in the image, where only the plane and the table are
    copied in. Walking up to the first `libs/packages/contract/devices.json` covers both
    without pinning a depth that is wrong in one of them; the env var is the
    escape hatch for a layout that is neither.
    """
    override = os.environ.get(DEVICE_TABLE_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        for prefix in (("contract",), ("packages", "contract")):
            candidate = parent.joinpath(*prefix, "devices.json")
            if candidate.is_file():
                return candidate
    return here.parents[2] / "contract" / "devices.json"


TABLE_PATH: Final = _table_path()


@dataclass(frozen=True)
class DeviceClass:
    """One card variant.

    Memory and form factor are part of the identity: `h100-80gb-sxm` and
    `h100-80gb-pcie` are different things to place against.

    `name` is the identifier and the only form that travels on the wire; `label`
    is the one to show a person. A class the table does not carry has no label
    of its own, and callers fall back to the name — see `label_for`.

    `devices_per_node` is how many of these share one coherent interconnect, and it is
    `None` unless the part is sold as a rack-scale domain: a GB300 NVL72 is 72 devices
    and one node, whatever number of chassis that is. `None` means the caller's default
    applies, and a cluster's own observed topology outranks both.

    `host_architecture` is the CPU the card is attached to, and empty means x86-64 — every
    class but the Grace superchips, which are `arm64`. It exists because some engines are built
    for one of the two only: vLLM-Omni ships amd64 images, so a diffusion workload on a GB200
    would be admitted, placed, and never start.
    """

    name: str
    label: str
    architecture: str
    vendor: str
    memory_mib: int
    form_factor: str
    aliases: dict[str, tuple[str, ...]]
    devices_per_node: int | None = None
    host_architecture: str = ""


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

    def label_for(self, name: str) -> str:
        """The name to show a person, falling back to the class itself.

        Hardware the table does not list still resolves to a slug and is still
        placed against, so this never returns empty — an unlabelled class reads
        as its own identifier rather than as nothing.
        """
        entry = self._by_name.get(name)
        return entry.label if entry is not None else name

    def largest(self) -> DeviceClass | None:
        """The biggest card the table names, or `None` when the table is empty.

        Sizing a first suggestion needs an upper bound on a single accelerator, and
        a constant in the caller is a second copy of the table that goes stale the
        week a bigger card ships. An empty table returns nothing and the caller
        decides what to do without one, rather than being handed a zero to divide by.
        """
        return max(self.classes, key=lambda c: c.memory_mib, default=None)

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
            label=entry.get("label") or entry["class"],
            architecture=entry.get("architecture", ""),
            vendor=entry["vendor"],
            memory_mib=int(entry["memory_mib"]),
            form_factor=entry["form_factor"],
            aliases={k: tuple(v) for k, v in entry.get("aliases", {}).items()},
            devices_per_node=entry.get("devices_per_node"),
            host_architecture=entry.get("host_architecture", ""),
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
