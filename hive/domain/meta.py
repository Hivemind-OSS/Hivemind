"""``normalize_meta`` — the ONE grammar gate for the opaque ``meta`` map carrier.

``meta`` is a small namespaced map of machine handles (``{"tool/attr": "value"}``)
that rides an episode and is served back verbatim on recall hits. It is
carried-not-interpreted — the exact ``anchor``/``kind`` idiom: the kernel enforces
the grammar ONCE here at the write boundary, stores the canonical serialized string,
and never parses it again — with ONE sanctioned exception: ``token_version`` /
``meta_version_histogram`` below read the value's ``<engine>-<kind>/<N>:`` VERSION
PREFIX (never the body) for aggregate observability
(``hive_health(include_meta_versions)``), so an operator can watch old token formats
age out (the meta envelope law's no-migration counterpart). Values are compact
handles (fingerprints, digests, ids), never payloads, and never embedding input.

Grammar (each violation raises ``BadMeta`` naming the reason — loud refusal, never a
silent drop): keys are namespaced lowercase ``tool/attr``; values are strings up to
``META_MAX_VALUE_LEN`` chars; at most ``META_MAX_KEYS`` keys; the canonical JSON
serialization (key-sorted, tight separators — byte-stable, so identical maps always
yield identical carrier bytes) stays within ``META_MAX_SERIALIZED`` bytes. The absent
forms ``None`` / ``""`` / ``{}`` normalize to ``""`` (no carrier — the column default).

PURE: stdlib ``json``/``re`` only. Total over ``object`` — isinstance-driven, never
indexes a non-dict (D8: a hostile shape refuses, it cannot crash the boundary).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any


class BadMeta(ValueError):
    """The loud grammar refusal — the message names the violated clause."""


META_MAX_KEYS = 16
META_MAX_VALUE_LEN = 256
META_MAX_SERIALIZED = 2048

# namespaced lowercase tool/attr: one '/', each side [a-z0-9] then [a-z0-9._-]*
_META_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")


def normalize_meta(raw: object) -> str:
    """Shape ``raw`` into the canonical serialized carrier string, or raise ``BadMeta``.

    ``None`` / ``""`` / ``{}`` → ``""`` (absent). A conforming dict → canonical JSON
    (``sort_keys`` + tight separators). Anything else refuses. // O(keys) time."""
    if raw is None or raw == "" or (isinstance(raw, dict) and not raw):
        return ""
    if not isinstance(raw, dict):
        raise BadMeta(
            f"meta must be a map of 'tool/attr' -> str handle, got {type(raw).__name__}"
        )
    if len(raw) > META_MAX_KEYS:
        raise BadMeta(f"meta has {len(raw)} keys (max {META_MAX_KEYS})")
    for k, v in raw.items():
        if not isinstance(k, str) or _META_KEY_RE.match(k) is None:
            raise BadMeta(f"bad meta key {k!r} (want namespaced lowercase 'tool/attr')")
        if not isinstance(v, str):
            raise BadMeta(f"meta value for {k!r} must be a str, got {type(v).__name__}")
        if len(v) > META_MAX_VALUE_LEN:
            raise BadMeta(
                f"meta value for {k!r} is {len(v)} chars (max {META_MAX_VALUE_LEN})"
            )
    out = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    n_bytes = len(out.encode("utf-8"))
    if n_bytes > META_MAX_SERIALIZED:
        raise BadMeta(f"serialized meta is {n_bytes} bytes (max {META_MAX_SERIALIZED})")
    return out


def token_version(value: str) -> str | None:
    """The `<N>` of a `<engine>-<kind>/<N>:<body>` token value — prefix-only read, never the
    body (the ONE opacity carve-out). None for a value with no ':' separator, no '/' before
    it, or a non-digit version segment — the caller buckets those as malformed."""
    head, sep, _body = value.partition(":")
    if not sep:
        return None
    _prefix, slash, version = head.rpartition("/")
    return version if (slash and version.isdigit()) else None


def meta_version_histogram(
    serialized_metas: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Fold serialized meta carriers into the per-key token-version histogram:
    `{key: {"versions": {"<N>": n}, "absent": a[, "malformed": m]}}` over the rows given
    (the caller supplies the live corpus). `absent` = rows carrying no value for that key
    (always present); `malformed` = values with no parseable version prefix (present only
    when nonzero); a key appears only once >=1 row carries it. Total over any input: an
    unparseable serialized row contributes no keys (it cannot name one) — never raises."""
    total = 0
    versions: dict[str, Counter[str]] = {}
    carriers: dict[str, int] = {}
    malformed: dict[str, int] = {}
    for serialized in serialized_metas:
        total += 1  # every row counts, carrier or not
        if not isinstance(serialized, str) or not serialized:
            continue
        try:
            parsed = json.loads(serialized)
        except Exception:
            continue  # a corrupt carrier names no key
        if not isinstance(parsed, dict):
            continue
        for key, value in parsed.items():
            carriers[key] = carriers.get(key, 0) + 1
            version = token_version(value) if isinstance(value, str) else None
            if version is None:
                malformed[key] = malformed.get(key, 0) + 1
            else:
                versions.setdefault(key, Counter())[version] += 1
    out: dict[str, dict[str, Any]] = {}
    for key, carried in carriers.items():
        entry: dict[str, Any] = {
            "versions": dict(versions.get(key, Counter())),
            "absent": total - carried,
        }
        if malformed.get(key, 0):
            entry["malformed"] = malformed[key]
        out[key] = entry
    return out
