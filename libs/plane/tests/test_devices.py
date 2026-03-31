"""The device-class dictionary, and that it means the same thing in both languages.

The Go side has its own copy of these cases in `libs/operator/internal/devices`.
The normalization rule is the only thing keeping two hand-written loaders on one
table, so it is asserted here character by character rather than in spirit.
"""

import json
import logging

from pathlib import Path

import pytest

from app.core.devices import TABLE_PATH
from app.core.devices import DIALECT_GFD
from app.core.devices import DIALECT_DCGM
from app.core.devices import DeviceClass
from app.core.devices import DeviceTable
from app.core.devices import load
from app.core.devices import slug
from app.core.devices import table
from app.core.devices import normalize


GO_TABLE = Path(__file__).resolve().parents[3] / "libs" / "operator" / "internal" / "devices" / "table.json"


def test_the_contract_file_is_what_the_operator_embeds():
    assert json.loads(TABLE_PATH.read_text()) == json.loads(GO_TABLE.read_text())


def test_the_seed_card_resolves_from_the_live_fleets_own_string():
    found = table().lookup(DIALECT_DCGM, "NVIDIA RTX PRO 6000 Blackwell Server Edition")
    assert found is not None
    assert found.name == "rtx-pro-6000-96gb-server-edition"
    assert found.memory_mib == 97249
    assert found.form_factor == "server-edition"


@pytest.mark.parametrize(
    ("dialect", "product", "expected"),
    [
        (DIALECT_GFD, "NVIDIA-H100-80GB-HBM3", "h100-80gb-sxm"),
        (DIALECT_DCGM, "NVIDIA H100 80GB HBM3", "h100-80gb-sxm"),
        (DIALECT_GFD, "NVIDIA-H100-PCIe", "h100-80gb-pcie"),
        (DIALECT_GFD, "NVIDIA-A100-SXM4-80GB", "a100-80gb-sxm"),
        (DIALECT_DCGM, "NVIDIA A100 80GB PCIe", "a100-80gb-pcie"),
        (DIALECT_GFD, "NVIDIA-L40S", "l40s-48gb-pcie"),
    ],
)
def test_both_dialects_land_on_one_class(dialect, product, expected):
    assert table().resolve(dialect, product) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NVIDIA-H100-80GB-HBM3", "h100-80gb-hbm3"),
        ("NVIDIA H100 80GB HBM3", "h100-80gb-hbm3"),
        ("  NVIDIA   L40S  ", "l40s"),
        ("NVIDIA A100-PCIE-40GB", "a100-pcie-40gb"),
        ("", ""),
    ],
)
def test_normalization_absorbs_spelling_drift(raw, expected):
    assert normalize(raw) == expected


def test_unknown_hardware_falls_through_to_a_stable_slug(caplog):
    with caplog.at_level(logging.WARNING):
        assert table().resolve(DIALECT_GFD, "NVIDIA-Zephyr-4000-Ultra") == "zephyr-4000-ultra"
    assert "devices.unmapped" in caplog.text
    assert slug("NVIDIA-Zephyr-4000-Ultra") == "zephyr-4000-ultra"


def test_a_fallthrough_is_logged_once_not_once_per_loop(caplog):
    fresh = load()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            fresh.resolve(DIALECT_GFD, "NVIDIA-Zephyr-9000")
    assert caplog.text.count("devices.unmapped") == 1


def test_a_dialect_does_not_borrow_anothers_aliases():
    assert table().lookup("vllm", "NVIDIA H200") is None


def test_a_missing_table_is_not_fatal(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        empty = load(tmp_path / "nowhere.json")
    assert empty.classes == []
    assert empty.resolve(DIALECT_GFD, "NVIDIA-H100-80GB-HBM3") == "h100-80gb-hbm3"


def test_every_class_is_a_slug_and_names_its_memory():
    for entry in table().classes:
        assert entry.name == normalize(entry.name)
        assert entry.memory_mib > 0
        assert entry.form_factor
        assert set(entry.aliases) <= {DIALECT_GFD, DIALECT_DCGM}


def test_aliases_of_an_unknown_class_are_empty():
    assert DeviceTable([]).aliases("h100-80gb-sxm", DIALECT_DCGM) == ()
    assert DeviceClass("x", "nvidia", 1, "pcie", {}).aliases == {}
