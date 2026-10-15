"""The routine examples in the documentation must stay valid."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dokkan_farmer.routine import parse_routine

DOCS = Path(__file__).resolve().parent.parent / "docs" / "ROUTINES.md"


def yaml_blocks() -> list[str]:
    return re.findall(r"```yaml\n(.*?)```", DOCS.read_text(encoding="utf-8"), flags=re.DOTALL)


@pytest.mark.parametrize("block", [b for b in yaml_blocks() if b.lstrip().startswith("name:")])
def test_complete_examples_parse(block):
    routine = parse_routine(block)
    assert routine.rules


def test_there_are_examples():
    assert sum(b.lstrip().startswith("name:") for b in yaml_blocks()) >= 2
