"""P1.12 / M07 — the RulesBlock contract-that-cannot-lie + the trailer single-source.

A block missing markers / version embed / trailer-in-body / a binding hash is
UNCONSTRUCTABLE, so the recorded link can never claim content that was not installed.
``test_trailer_key_is_single_sourced`` ★ is the §11/§6.1#6 CONFIG_DRIFT guard (mutation:
hard-code the template literal → red).
"""
from __future__ import annotations

import hashlib
import re

import pytest

from hive.app.onboard import render_rules_block
from hive.domain.models import (
    RULES_BLOCK_END, RULES_BLOCK_START, RulesBlock, rules_block_version_marker,
)


def test_rules_block_markers_version_hash():
    block = render_rules_block("Hive-Trace", 1)
    assert RULES_BLOCK_START in block.rendered_text
    assert RULES_BLOCK_END in block.rendered_text
    assert rules_block_version_marker(1) in block.rendered_text
    assert block.block_version == 1
    assert block.block_hash == hashlib.sha256(block.rendered_text.encode("utf-8")).digest()


def test_rules_block_unconstructable_when_illformed():
    good = (f"{RULES_BLOCK_START}\n{rules_block_version_marker(1)}\n"
            f"trailer Hive-Trace here\n{RULES_BLOCK_END}")
    good_hash = hashlib.sha256(good.encode("utf-8")).digest()
    # sanity: the well-formed block IS constructable
    RulesBlock(rendered_text=good, trailer_key="Hive-Trace", block_version=1,
               block_hash=good_hash)

    # (a) missing markers
    with pytest.raises(ValueError):
        body = "no markers but mentions Hive-Trace"
        RulesBlock(rendered_text=body, trailer_key="Hive-Trace", block_version=1,
                   block_hash=hashlib.sha256(body.encode()).digest())
    # (b) missing version embed (markers + trailer present, wrong/absent version line)
    with pytest.raises(ValueError):
        body = f"{RULES_BLOCK_START}\nHive-Trace\n{RULES_BLOCK_END}"
        RulesBlock(rendered_text=body, trailer_key="Hive-Trace", block_version=1,
                   block_hash=hashlib.sha256(body.encode()).digest())
    # (c) trailer_key absent from body (the single-source guard at the carrier)
    with pytest.raises(ValueError):
        body = f"{RULES_BLOCK_START}\n{rules_block_version_marker(1)}\nno trailer\n{RULES_BLOCK_END}"
        RulesBlock(rendered_text=body, trailer_key="Hive-Trace", block_version=1,
                   block_hash=hashlib.sha256(body.encode()).digest())
    # (d) hash does not bind body
    with pytest.raises(ValueError):
        RulesBlock(rendered_text=good, trailer_key="Hive-Trace", block_version=1,
                   block_hash=hashlib.sha256(b"different").digest())


def test_trailer_key_is_single_sourced():
    # The trailer is interpolated from the (config) stamp_trailer — never literal. A
    # custom trailer must appear verbatim in the rendered body and the placeholder must
    # be fully substituted; neither generation's default literal may leak in.
    block = render_rules_block("ZZZ-Custom-Trailer", 1)
    assert block.trailer_key == "ZZZ-Custom-Trailer"
    assert "ZZZ-Custom-Trailer" in block.rendered_text
    assert "<TRAILER_KEY>" not in block.rendered_text
    assert "Hive-Trace" not in block.rendered_text          # v1 literal does not leak
    assert "Hive-Credit" not in block.rendered_text         # v2 literal does not leak


def test_rules_block_v2_selective_credit_wording():
    # v2 credit section: examples-as-spec — one exact, copyable trailer line
    # (32-hex trace + episode ids) — and the selective-credit rule stated.
    block = render_rules_block("Hive-Credit", 2)
    text = block.rendered_text
    assert rules_block_version_marker(2) in text
    assert re.search(r"Hive-Credit: [0-9a-f]{32} \d+ \d+", text)
    assert "materially shaped" in text
    assert "nothing if none did" in text


def test_render_version_embed_tracks_block_version():
    block = render_rules_block("Hive-Trace", 3)
    assert rules_block_version_marker(3) in block.rendered_text
    assert rules_block_version_marker(1) not in block.rendered_text
    assert block.block_version == 3


def test_render_empty_trailer_fails_fast():
    with pytest.raises(ValueError):
        render_rules_block("", 1)
