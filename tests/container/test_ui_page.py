"""The self-contained operator page — grep-able design/originality/craft invariants.

`ui_page.PAGE_HTML` is ONE hermetic HTML string (inline CSS/JS, relative-URL fetches, no
external asset). These tests pin the visual-system anchors the design/originality/craft
criteria grade against, so a regression (marigold sprayed, a red state, an external asset,
a reset/restore affordance, a second animation) reds a test rather than silently drifting.
"""

from __future__ import annotations

import re

from hive.tools import ui_page

HTML = ui_page.PAGE_HTML
LOW = HTML.lower()


def test_page_is_a_self_contained_html_document():
    assert isinstance(HTML, str)
    assert LOW.lstrip().startswith("<!doctype html>")
    assert "<style>" in LOW and "<script>" in LOW  # inline CSS + JS, one document


# ── d1: the white/black/yellow palette; marigold restrained to a single token ──
def test_palette_tokens_present():
    for hexcode in ("#FFFFFF", "#FAFAF7", "#0A0A0A", "#6B6B6B", "#EAEAEA", "#FFC400"):
        assert hexcode in HTML


def test_marigold_is_a_single_token_never_sprayed():
    # #FFC400 lives in exactly ONE place (the :root token); every marigold surface references
    # it via var(--marigold), so the accent can never be sprayed by a stray literal.
    assert HTML.count("#FFC400") == 1
    # applied to exactly the beacon(healthy) + primary button + seat-count numeral + focus ring.
    assert HTML.count("var(--marigold)") >= 4


def test_seat_count_numeral_wins_marigold_over_row_v():
    # d1: the seat-count numeral is one of the three marigold accents. `.row .v` (specificity
    # 0,2,0) out-specifies a bare `.seat-count` (0,1,0), so the numeral would render INK; the
    # marigold colour must ride the compound `.row .v.seat-count` (0,3,0) to actually win.
    block = re.search(r"\.row \.v\.seat-count\s*\{([^}]*)\}", HTML)
    assert block is not None, (
        "seat-count colour must ride `.row .v.seat-count` (beats `.row .v`)"
    )
    assert "var(--marigold)" in block.group(1)


def test_exactly_one_marigold_primary_button_rest_are_ghost():
    # d1: marigold fills EXACTLY ONE primary button — "Add a seat". Every other action button
    # (Backup now, Start, Stop) is the ghost/outline base `.btn` (ink on paper, ink hairline
    # border, NO marigold fill) — a clean single-primary hierarchy.
    assert HTML.count('class="btn btn--primary"') == 1  # a single marigold primary
    assert re.search(
        r'class="btn btn--primary"\s+id="add-seat"', HTML
    )  # and it is add-seat
    for cid in ("backup", "start", "stop", "tunnel-toggle", "do-restore"):
        m = re.search(r'class="([^"]*)"\s+id="' + cid + '"', HTML)
        assert m is not None, f"button #{cid} not found"
        assert "btn--primary" not in m.group(1), (
            f"#{cid} must be a secondary/ghost button"
        )


def test_no_red_or_green_health_state_anywhere():
    # o1: strict white/black/yellow — health is the beacon's shape/fill, NEVER a red/green state.
    for token in (
        "#ff0000",
        "#f00 ",
        "#00ff00",
        "#0f0 ",
        "rgb(255,0,0)",
        "rgb(0,128,0)",
        "tomato",
        "crimson",
        "seagreen",
        "limegreen",
        "indianred",
        "forestgreen",
    ):
        assert token not in LOW


# ── o2: reads as a signed console, NOT a templated admin scaffold ──
def test_no_template_scaffold_markers():
    assert "linear-gradient" not in LOW  # no gradient stat cards
    assert "box-shadow" not in LOW  # no drop-shadowed boxes
    assert "sidebar" not in LOW and "topbar" not in LOW and "navbar" not in LOW


# ── o1 + d4: exactly one animation, gated by prefers-reduced-motion ──
def test_single_keyframes_animation_gated_by_reduced_motion():
    assert LOW.count("@keyframes") == 1  # the pulse is the ONLY animation
    assert "prefers-reduced-motion" in LOW
    assert "beacon" in LOW


# ── d2: mono data + heavy-sans headings + wide-tracked uppercase eyebrows ──
def test_typography_system():
    assert "monospace" in LOW  # mono stack for data/labels/logs
    assert (
        "letter-spacing" in LOW and "uppercase" in LOW
    )  # wide-tracked uppercase eyebrows
    for eyebrow_id in ('id="eb-server"', 'id="eb-tokens"', 'id="eb-logs"'):
        assert eyebrow_id in HTML  # SERVER / TOKENS / LOGS cards


# ── d4: visible marigold focus ring on every control ──
def test_focus_ring_present():
    assert ":focus-visible" in LOW
    assert "outline" in LOW


# ── d3 + d5: honest interface-voice copy; the shown-once caption ──
def test_interface_voice_and_shown_once_caption():
    assert "shown once" in LOW and "copy it now" in LOW
    assert "add a seat" in LOW


def test_no_reset_affordance_reset_stays_absent():
    # f5: RESET is absent from the page BY CONSTRUCTION — no button, link, or fetch (a graded
    # invariant). restore IS present now (guarded, typed confirm), but the scary volume-destroy
    # verbs stay absent — restore "replaces" the live store, it never destroys the volume.
    assert "reset" not in LOW
    for destructive in ("destroy", "wipe", "-v "):
        assert destructive not in LOW


# ── c4: stdlib-only, relative fetches, no external asset ──
def test_relative_fetches_no_external_asset():
    # no absolute-URL fetch and no CDN asset — the page is hermetic, served off loopback.
    assert "http://" not in LOW and "https://" not in LOW
    assert "src=" not in LOW  # no external <script src>/<img src>
    assert "//cdn" not in LOW and "unpkg" not in LOW and "jsdelivr" not in LOW


def test_all_api_routes_are_wired_in_the_page():
    for path in (
        "/api/status",
        "/api/tokens",
        "/api/tokens/revoke",
        "/api/backup",
        "/api/backups",
        "/api/restore",
        "/api/lifecycle",
        "/api/logs",
    ):
        assert path in HTML


def test_tunnel_and_restore_controls_present():
    # C-features on the page: the SERVER card gains a tunnel Activate/Deactivate control, and a
    # dedicated RESTORE section gains a snapshot picker + a Restore button behind a typed confirm.
    assert 'id="tunnel-toggle"' in HTML  # tunnel activate/deactivate control
    assert 'id="eb-restore"' in HTML  # the dedicated RESTORE card eyebrow
    assert 'id="backup-pick"' in HTML  # the snapshot picker (populated live)
    assert 'id="do-restore"' in HTML  # the restore button
    assert "safety snapshot" in LOW  # honest warning: a snapshot is taken first
    assert '"restore"' in HTML  # the typed-confirm word
