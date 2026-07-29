"""Ratchets on the operator doc surface.

A skill that ships without an index row, an index row naming a skill that is not
on disk, a client-side surface no operator doc points at, or a restored "there is
nothing to install" absolute — each is a failing test here rather than something
an operator discovers at install time. Every one of these has happened.

These assert STRUCTURE and REFERENCE only. Rewording any sentence in any of these
files is free; dropping a reference, or restoring a claim the shipped harness
falsifies, is not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"

# The surface whose existence obliges the operator docs to mention it.
CLIENT_SIDE = "harnesses"

# Absolutes a shipped skill must not carry while that surface exists. These match
# the DEFECT, never the fix: rephrasing the correct claim cannot red this.
FALSIFIED_ABSOLUTES = ("nothing to install", "only client-side step")


def _shipped_skills() -> set[str]:
    return {p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()}


def _index_body() -> str:
    return (SKILLS / "README.md").read_text(encoding="utf-8")


def _indexed_skills() -> set[str]:
    return set(re.findall(r"\[`([a-z0-9-]+)`\]\(\1/SKILL\.md\)", _index_body()))


def _activated_skills() -> set[str]:
    match = re.search(r"^for s in (.+?); do$", _index_body(), re.MULTILINE)
    assert match, "skills/README.md lost its activation loop"
    return set(match.group(1).split())


def test_the_skills_index_and_the_tree_agree_in_both_directions() -> None:
    shipped, indexed = _shipped_skills(), _indexed_skills()
    assert shipped == indexed, (
        f"skills/README.md table is out of step with skills/: "
        f"on disk but unindexed={sorted(shipped - indexed)}, "
        f"indexed but absent={sorted(indexed - shipped)}"
    )


def test_every_shipped_skill_is_in_the_activation_loop() -> None:
    shipped, activated = _shipped_skills(), _activated_skills()
    assert shipped == activated, (
        f"skills/README.md activation loop is out of step with skills/: "
        f"missing={sorted(shipped - activated)}, stale={sorted(activated - shipped)}"
    )


def test_the_client_side_surface_is_referenced_where_an_operator_would_look() -> None:
    if not (ROOT / CLIENT_SIDE).is_dir():
        return  # the surface is gone; nothing is owed a reference
    for rel in ("README.md", "llms.txt", "HIVE-ADMIN.md"):
        assert CLIENT_SIDE in (ROOT / rel).read_text(encoding="utf-8"), (
            f"{rel} never mentions {CLIENT_SIDE}/ — an operator reading it cannot "
            f"discover the client-side surface exists"
        )
    assert any(
        CLIENT_SIDE in (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for name in _shipped_skills()
    ), (
        f"no shipped skill references {CLIENT_SIDE}/ — there is no runbook for installing it"
    )


def test_no_shipped_skill_says_there_is_nothing_to_install() -> None:
    if not (ROOT / CLIENT_SIDE).is_dir():
        return  # with no client-side surface the absolute is true again
    for name in sorted(_shipped_skills()):
        body = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8").lower()
        for absolute in FALSIFIED_ABSOLUTES:
            assert absolute not in body, (
                f"skills/{name}/SKILL.md claims {absolute!r}, which {CLIENT_SIDE}/ "
                f"falsifies — an agent reading it will tell an operator the harness "
                f"needs no install"
            )
