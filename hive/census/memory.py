"""Institutional-memory context: the census as an ordinary hive_recall consumer.

Per touched subject, ask the loopback hive's read door what the fleet already
learned and render the confident answers as trust-labelled REFERENCE entries
for the receipt's optional context block. The client holds no privileged read
path — one plain JSON-RPC 2.0 ``tools/call`` POST per subject over urllib —
and it is label-blind: trust, polarity, and kind ride verbatim from the
recall envelope, no tier is filtered, nothing here interprets a label.
Context informs the human reading the receipt; it never certifies, never
gates, and never becomes an evidence line.

Memory is a side-channel, so the fetch fails OPEN as a unit: any transport or
envelope fault — refused connection, timeout, HTTP error, unparseable body —
yields no context and one stderr note, and the receipt build proceeds
byte-identical minus the block. A half-healthy door never half-reports.
Malformed individual hits inside a healthy envelope are skipped, never fatal,
and unknown hit keys (a newer server's enrichments) are ignored.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_TEXT_CAP = 400
_PRIORITY_DRIFTS = frozenset({"breaking", "removed"})


class _MemoryFault(Exception):
    """Internal: one transport or envelope fault aborts the whole fetch."""


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """One trust-labelled fleet-memory reference about a touched subject."""

    episode_id: int
    trust: str
    polarity: str
    kind: str
    anchor: str
    ts: int
    sim: float
    text: str


def order_subjects(symbols: Sequence[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Unique (path, symbol) query order from (path, symbol, drift) triples.

    Highest-stakes first: subjects carrying breaking/removed drift precede
    all others, lexicographic within each band — the subject cap truncates
    the tail, so order IS the priority policy. A subject seen with both a
    priority and a benign drift keeps its priority slot.
    """
    banded = sorted(
        {
            (0 if drift in _PRIORITY_DRIFTS else 1, path, symbol)
            for path, symbol, drift in symbols
        }
    )
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, path, symbol in banded:
        if (path, symbol) not in seen:
            seen.add((path, symbol))
            ordered.append((path, symbol))
    return ordered


def _recall_payload(
    hive_url: str, query: str, timeout_s: float, request_id: int
) -> dict[str, Any]:
    """One hive_recall round trip: the tool payload dict, or _MemoryFault."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "hive_recall", "arguments": {"query": query}},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        hive_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hive-Agent-Id": "hive-census",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
        envelope = json.loads(raw.decode("utf-8"))
    except Exception as error:
        raise _MemoryFault(f"{type(error).__name__}: {error}") from error
    if not isinstance(envelope, dict) or envelope.get("error") is not None:
        raise _MemoryFault("JSON-RPC error envelope")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise _MemoryFault("tool result missing or marked isError")
    content = result.get("content")
    first = content[0] if isinstance(content, list) and content else None
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        raise _MemoryFault("tool result carries no text content")
    try:
        payload = json.loads(text)
    except Exception as error:
        raise _MemoryFault(f"unparseable tool payload: {error}") from error
    if not isinstance(payload, dict):
        raise _MemoryFault("tool payload is not an object")
    return payload


def _label(value: object) -> str:
    """A label passes through verbatim when string-shaped; else fail-safe ''."""
    return value if isinstance(value, str) else ""


def _entry_from_hit(hit: object) -> MemoryContext | None:
    """Coerce one recall hit defensively; None when identity or text is unusable."""
    if not isinstance(hit, dict):
        return None
    episode_id = hit.get("episode_id")
    if isinstance(episode_id, bool) or not isinstance(episode_id, int):
        return None
    text = hit.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    ts = hit.get("ts")
    sim = hit.get("sim")
    sim_value = (
        float(sim)
        if isinstance(sim, (int, float))
        and not isinstance(sim, bool)
        and math.isfinite(sim)
        else 0.0
    )
    return MemoryContext(
        episode_id=episode_id,
        trust=_label(hit.get("trust")),
        polarity=_label(hit.get("polarity")),
        kind=_label(hit.get("kind")),
        anchor=_label(hit.get("anchor")),
        ts=ts if isinstance(ts, int) and not isinstance(ts, bool) else 0,
        sim=sim_value,
        text=text[:_TEXT_CAP],
    )


def fetch_institutional_context(
    hive_url: str,
    subjects: Sequence[tuple[str, str]],
    *,
    cap_subjects: int = 8,
    cap_total: int = 10,
    timeout_s: float = 5.0,
) -> list[MemoryContext]:
    """Recall fleet memory per subject; capped, deduped, trust-labelled entries.

    One ``f"{path} {symbol}"`` content query per unique subject (the first
    ``cap_subjects`` in the given order). An abstaining recall contributes
    nothing — an abstained envelope's hits are never trusted. Entries dedupe
    by episode id across subjects and stop at ``cap_total``. ANY transport or
    envelope fault yields ``[]`` with one stderr note: the receipt build
    never fails — and never half-reports — on memory.
    """
    unique: list[tuple[str, str]] = []
    seen_subjects: set[tuple[str, str]] = set()
    for subject in subjects:
        if subject not in seen_subjects:
            seen_subjects.add(subject)
            unique.append(subject)
    entries: list[MemoryContext] = []
    seen_ids: set[int] = set()
    try:
        for index, (path, symbol) in enumerate(unique[: max(cap_subjects, 0)], 1):
            if len(entries) >= cap_total:
                break
            payload = _recall_payload(hive_url, f"{path} {symbol}", timeout_s, index)
            if payload.get("abstained"):
                continue
            hits = payload.get("reference_context")
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if len(entries) >= cap_total:
                    break
                entry = _entry_from_hit(hit)
                if entry is None or entry.episode_id in seen_ids:
                    continue
                seen_ids.add(entry.episode_id)
                entries.append(entry)
    except _MemoryFault as fault:
        print(
            f"hive-census: institutional memory unavailable ({fault}); "
            "building the receipt without context",
            file=sys.stderr,
        )
        return []
    return entries
