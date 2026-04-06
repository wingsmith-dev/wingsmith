"""Round-trips the golden corpus in `libs/packages/contract/fixtures/`.

This is the replacement for codegen (ADR 0001). Every fixture must decode into a registered
Struct and re-encode to a semantically identical document, and the corpus and the registry must
cover exactly the same set of types.
"""

import json

from typing import Any
from typing import get_args
from typing import get_origin
from typing import get_type_hints
from pathlib import Path

import pytest
import msgspec

from msgspec import Struct
from msgspec import json as msgjson

from app.hub.messages import ID_PREFIXES
from app.hub.messages import MESSAGE_TYPES
from app.hub.messages import ENVELOPE_VERSION
from app.hub.messages import TIMESTAMP_PATTERN
from app.hub.messages import UNSEQUENCED_TYPES
from app.hub.messages import decode
from app.hub.messages import encode


FIXTURES = Path(__file__).resolve().parents[2] / "packages" / "contract" / "fixtures"

FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))

FIXTURE_NAMES = {path.stem for path in FIXTURE_FILES}


def normalise(value: Any) -> Any:
    """Drop null-valued keys recursively.

    PROTOCOL §2 treats an absent optional field and an explicit null as the same thing, so
    semantic equality must too.
    """
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def test_corpus_is_not_empty() -> None:
    assert FIXTURE_FILES


def test_every_type_has_a_fixture() -> None:
    missing = sorted(set(MESSAGE_TYPES) - FIXTURE_NAMES)
    assert not missing, f"registered types with no fixture: {missing}"


def test_every_fixture_has_a_type() -> None:
    unknown = sorted(FIXTURE_NAMES - set(MESSAGE_TYPES))
    assert not unknown, f"fixtures with no registered type: {unknown}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_round_trips(path: Path) -> None:
    original = json.loads(path.read_text())
    envelope = decode(path.read_bytes())
    assert envelope.type == path.stem, "fixture filename must equal its envelope type"
    reencoded = json.loads(encode(envelope))
    assert normalise(reencoded) == normalise(original)


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_payload_is_exhaustive(path: Path) -> None:
    """A fixture must not carry payload fields the Struct silently discards."""
    original = json.loads(path.read_text())
    struct = MESSAGE_TYPES[original["type"]]
    known = set(struct.__struct_fields__)
    present = set(original["payload"])
    assert present <= known, f"payload fields not in {struct.__name__}: {sorted(present - known)}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_envelope_conventions(path: Path) -> None:
    doc = json.loads(path.read_text())
    assert doc["v"] == ENVELOPE_VERSION
    assert len(doc["id"]) == 26
    assert TIMESTAMP_PATTERN.match(doc["ts"])
    assert isinstance(doc["payload"], dict)
    if doc["type"] in UNSEQUENCED_TYPES:
        assert doc.get("seq") is None, "unsequenced frames must not carry seq"
    else:
        assert doc["seq"] >= 1


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_entity_ids_are_prefixed_ulids(path: Path) -> None:
    doc = json.loads(path.read_text())

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value[:4] in ID_PREFIXES:
            assert len(value) == 30, f"{value} is not a prefixed 26-character ULID"

    walk(doc["payload"])


def populated_paths(value: Any, prefix: str) -> set[str]:
    """Dotted paths of every non-null leaf and container in a decoded document."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if item is None:
                continue
            found.add(f"{prefix}.{key}")
            found |= populated_paths(item, f"{prefix}.{key}")
    elif isinstance(value, list):
        for item in value:
            found |= populated_paths(item, prefix)
    return found


def struct_paths(struct: type[Struct], prefix: str) -> set[str]:
    """Dotted paths of every field on a Struct, recursing into nested Structs."""
    found: set[str] = set()
    hints = get_type_hints(struct)
    for field in struct.__struct_fields__:
        path = f"{prefix}.{field}"
        found.add(path)
        for arg in (hints[field], *get_args(hints[field])):
            inner = get_args(arg)[0] if get_origin(arg) is list else arg
            if isinstance(inner, type) and issubclass(inner, Struct):
                found |= struct_paths(inner, path)
    return found


def test_optional_fields_are_exercised_somewhere() -> None:
    """Every field on every registered Struct is non-null in at least one fixture."""
    seen: set[str] = set()
    for path in FIXTURE_FILES:
        doc = json.loads(path.read_text())
        seen |= populated_paths(doc["payload"], doc["type"])

    expected: set[str] = set()
    for type_name, struct in MESSAGE_TYPES.items():
        expected |= struct_paths(struct, type_name)

    missing = sorted(expected - seen)
    assert not missing, f"payload fields never populated in any fixture: {missing}"


def test_decode_rejects_a_wrong_payload_shape() -> None:
    broken = msgjson.encode(
        {"v": 1, "type": "welcome", "id": "01J9X2E5G7QK8V3B0YHN4TZC6M", "ts": "2026-08-19T14:02:11.482Z", "payload": {}}
    )
    with pytest.raises(msgspec.ValidationError):
        decode(broken)
