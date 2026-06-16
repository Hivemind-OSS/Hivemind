"""P1.12 / M07 — the RulesBlock contract-that-cannot-lie.

A block missing markers / version embed / a binding hash is UNCONSTRUCTABLE, so the
recorded link can never claim content that was not installed. The rendered block carries
NO commit-trailer / credit instruction (the credit producer was cut).
"""
from __future__ import annotations

import hashlib

import pytest

from hive.app.onboard import render_rules_block
from hive.domain.models import (
    RULES_BLOCK_END, RULES_BLOCK_START, RulesBlock, rules_block_version_marker,
)


def test_rules_block_markers_version_hash():
    block = render_rules_block(1)
    assert RULES_BLOCK_START in block.rendered_text
    assert RULES_BLOCK_END in block.rendered_text
    assert rules_block_version_marker(1) in block.rendered_text
    assert block.block_version == 1
    assert block.block_hash == hashlib.sha256(block.rendered_text.encode("utf-8")).digest()


def test_rules_block_unconstructable_when_illformed():
    good = (f"{RULES_BLOCK_START}\n{rules_block_version_marker(1)}\n"
            f"body here\n{RULES_BLOCK_END}")
    good_hash = hashlib.sha256(good.encode("utf-8")).digest()
    # sanity: the well-formed block IS constructable
    RulesBlock(rendered_text=good, block_version=1, block_hash=good_hash)

    # (a) missing markers
    with pytest.raises(ValueError):
        body = "no markers here"
        RulesBlock(rendered_text=body, block_version=1,
                   block_hash=hashlib.sha256(body.encode()).digest())
    # (b) missing version embed (markers present, wrong/absent version line)
    with pytest.raises(ValueError):
        body = f"{RULES_BLOCK_START}\nbody\n{RULES_BLOCK_END}"
        RulesBlock(rendered_text=body, block_version=1,
                   block_hash=hashlib.sha256(body.encode()).digest())
    # (c) hash does not bind body
    with pytest.raises(ValueError):
        RulesBlock(rendered_text=good, block_version=1,
                   block_hash=hashlib.sha256(b"different").digest())


def test_rules_block_has_no_credit_trailer():
    # the producer strip: the rendered block carries NO commit-trailer / credit instruction.
    text = render_rules_block(2).rendered_text
    assert "Hive-Credit" not in text and "Hive-Trace" not in text
    assert "Credit your work" not in text
    assert "<TRAILER_KEY>" not in text


def test_render_version_embed_tracks_block_version():
    block = render_rules_block(3)
    assert rules_block_version_marker(3) in block.rendered_text
    assert rules_block_version_marker(1) not in block.rendered_text
    assert block.block_version == 3
