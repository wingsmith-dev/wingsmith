"""Profiles and clips ship inside the package, so an install outside the checkout has them."""

from __future__ import annotations

from pathlib import Path

import hgrconform

from hgrconform import cli
from hgrconform.probes import video
from hgrconform.profile import load_profile


PACKAGE = Path(hgrconform.__file__).resolve().parent


def test_profiles_ship_inside_the_package() -> None:
    assert cli.PROFILES_DIR.resolve().is_relative_to(PACKAGE)
    names = {path.stem for path in cli.PROFILES_DIR.glob("*.toml")}
    assert {"example", "gateway"} <= names


def test_every_committed_profile_loads() -> None:
    for path in cli.PROFILES_DIR.glob("*.toml"):
        assert load_profile(path).name == path.stem


def test_a_profile_resolves_by_name_from_any_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.profile_path("example") == cli.PROFILES_DIR / "example.toml"


def test_video_clips_ship_inside_the_package() -> None:
    assert video.ASSETS.resolve().is_relative_to(PACKAGE)
    for clip in (video.BASE, video.LONG, *video.DURATIONS, *video.RATES, *video.SIZES):
        assert clip.read()[4:8] == b"ftyp", clip.name
