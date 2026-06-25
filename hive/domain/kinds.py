"""The kind taxonomy registry — the single source for the structured-memory vocabulary.

PURE DATA: this module imports nothing (the purity gate forbids sqlite3|torch|subprocess|
os|git|time anywhere in hive/domain/; pure constants are fine). Every surface that needs the
vocabulary PROJECTS from here — tool_defs builds the ``kind`` enum, onboard_ref renders the
served prose, the store builds its CHECK clause, models/admission validate against
``KIND_NAMES`` — so the advertised, enforced, served, and stored copies cannot drift.

Two orthogonal axes carry a memory's structure (both carried-not-interpreted labels that
ride every recall hit, neither embedded): ``kind`` (here) = the category of knowledge;
``polarity`` (do|dont|neutral, in models.py) = the directive valence. An antipattern is
``polarity='dont'`` over a convention/design_choice — NOT a separate kind. ``kind`` is never
a recall filter (read-side scoping is out of scope); it labels hits and, because the writer
puts the discriminating terms in the body, sharpens recall only via content similarity.
"""
from __future__ import annotations

# The fail-safe, under-claiming default: a capture that names no kind is a generic ``note``,
# never mislabelled as a sharper kind.
DEFAULT_KIND = "note"

# kind -> {gloss, default_polarity, template}. ``gloss`` = the one-line "question it answers"
# (the compact call-adjacent guidance); ``template`` = the body shape an agent fills (the
# served reference); ``default_polarity`` = the valence the kind usually carries.
KINDS: dict[str, dict[str, str]] = {
    "bug": {
        "gloss": "a defect — what broke / is broken, why, and the fix or workaround",
        "default_polarity": "neutral",
        "template": ("lead with OPEN — or RESOLVED —; then symptom -> root cause -> "
                     "fix | workaround | (none yet, surrounding context only)"),
    },
    "gotcha": {
        "gloss": "a surprising-but-correct behavior, footgun, or load-bearing invariant",
        "default_polarity": "dont",
        "template": "the surprising behavior -> why it holds -> what relies on it",
    },
    "convention": {
        "gloss": "the house way to do a recurring thing here",
        "default_polarity": "do",
        "template": "the rule -> where it applies -> why (antipattern = polarity=dont)",
    },
    "design_choice": {
        "gloss": "a decision, why X over Y, and where it governs",
        "default_polarity": "neutral",
        "template": "the decision -> why X over Y -> the files/modules it governs",
    },
    "contract": {
        "gloss": "what a boundary guarantees and what its callers assume",
        "default_polarity": "neutral",
        "template": "component -> guarantees -> caller assumptions (change here -> breaks there)",
    },
    "dead_end": {
        "gloss": "an approach tried here and abandoned, and why (so the cost isn't re-paid)",
        "default_polarity": "dont",
        "template": "the approach tried -> why it failed -> what to do instead",
    },
    "env_fact": {
        "gloss": "an operational / process fact not derivable from the code",
        "default_polarity": "neutral",
        "template": "the fact -> when/where it applies (deploy step, precondition, flaky cause)",
    },
    "note": {
        "gloss": "a durable insight that doesn't fit a sharper kind (the default)",
        "default_polarity": "neutral",
        "template": "one sharp fact: WHAT -> WHERE -> WHY",
    },
}

# The single membership set every enforcer reads (tool schema enum, DDL CHECK, __post_init__).
KIND_NAMES: frozenset[str] = frozenset(KINDS)

POLAR_LANGUAGE_RULE = (
    "Use hard, unambiguous polar language so a reader separates directive from context by "
    "verb class alone: positive = do/always/use/prefer/apply; negative = don't/never/avoid; "
    "strictly neutral = is/exists/lives in/relates to (no action verb). Condition directives "
    "with neutral context, condition first: 'In <context>, always <directive>.' Set polarity "
    "to the primary valence; neutral means the memory prescribes no action."
)

DENSITY_RULE = (
    "Write one sharp, self-contained fact: WHAT + WHERE (file/module/symbol) + WHY (the "
    "non-obvious bit). Be dense and specific (name the symbol/error/condition); do NOT pad or "
    "repeat boilerplate -- shared scaffolding and verbosity flatten the embeddings and make "
    "recall abstain. One sharp fact beats a paragraph."
)

QUERY_GUIDANCE = (
    "Writing a hive_recall query: search the CONTENT, not the label -- recall matches your "
    "query against the stored body (kind/anchor are NOT embedded), so 'bug' will not surface "
    "bugs; name the symptom/symbol/behavior. Be dense and specific: vague queries flatten "
    "similarities and the gate ABSTAINS rather than guess. Split a multi-part need into a SET of "
    "SINGLE-POINTED queries -- one intent (one symptom, symbol, or claim) per query -- and run one "
    "hive_recall per intent; never bundle questions into one query (a bundled multi-question query "
    "dilutes toward the centroid -> abstain). Phrase as the claim you "
    "expect, not only a question. Recall first and on every error/surprise; treat hits as "
    "reference; empty = no confident match, so proceed, never invent. Honor each hit's "
    "polarity (never follow a 'dont' as a 'do'), kind, and anchor."
)


def render_taxonomy() -> str:
    """Assemble the served write-side discipline (the kinds + their body templates + the polar
    and density rules) from ``KINDS``, so every served surface reads ONE renderer and cannot
    drift from the enforced enum."""
    lines = ["WHAT TO CAPTURE — pick a kind (it rides every recall hit; it is NOT a recall filter):"]
    for name, spec in KINDS.items():
        lines.append(f"- {name}: {spec['gloss']}")
        lines.append(f"    body: {spec['template']}")
    lines.append("")
    lines.append("POLAR LANGUAGE: " + POLAR_LANGUAGE_RULE)
    lines.append("DENSITY: " + DENSITY_RULE)
    return "\n".join(lines)
