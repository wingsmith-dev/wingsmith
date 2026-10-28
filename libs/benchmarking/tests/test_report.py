"""The report page treats everything in a suite as text."""

from __future__ import annotations

import re
import json

from pathlib import Path

from wingsbench.report import render
from wingsbench.report.html import embedded_json


HOSTILE = "</script><img src=x onerror=alert(1)>&"


def _suite() -> dict:
    return {
        "run_id": HOSTILE,
        "plan_name": "p",
        "models": [HOSTILE],
        "notes": [HOSTILE],
        "measurements": [
            {
                "cell_id": "c",
                "harness": "aiperf",
                "model": HOSTILE,
                "status": HOSTILE,
                "notes": [HOSTILE],
                "readings": {},
                "missing": [HOSTILE],
            }
        ],
    }


def test_embedded_json_cannot_close_its_script_element() -> None:
    text = embedded_json({"note": HOSTILE})

    assert "<" not in text and ">" not in text and "&" not in text
    assert json.loads(text) == {"note": HOSTILE}


def test_the_page_carries_no_raw_markup_from_the_suite(tmp_path: Path) -> None:
    page = render(_suite(), tmp_path / "report.html").read_text()

    assert "<img" not in page
    assert page.count("</script>") == 2
    assert "<title>wingsbench: &lt;/script&gt;" in page


def test_every_template_escapes_suite_strings(tmp_path: Path) -> None:
    """A lint over the page's own script, since no test here runs it.

    Suite strings interpolated into a template go through `esc()`. Tooltip titles are
    the exception that proves it: `hoverable` escapes its own arguments.
    """
    page = render(_suite(), tmp_path / "report.html").read_text()
    field = r"\$\{(?:s|r|D\.suite|knee\.breach)\.(?:model|harness|status|stage|why|source|run_id)\}"
    raw = [line for line in page.splitlines() if re.search(field, line) and "hoverable(" not in line]

    assert raw == []
