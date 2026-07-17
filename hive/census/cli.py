"""Operator front door: argv in, one unsigned receipt envelope on disk out.

Pure wiring over the composer modules, in the safety-ordered sequence: the
matrix scratch pin lands before any matrix import can read MATRIX_OUT; the diff
is opened once and every engine consumes its frozen carriers; the envelope is
written atomically (temp + rename) so a failed build can never leave a partial
artifact behind. Receipts are emitted unsigned by policy — the ingest-door trust
boundary is transport/auth, not a receipt signature — so no key is loaded or
required anywhere. Evidence failures inside the pipeline surface as a one-line
stderr message and EX_SOFTWARE — never a stack trace, never a partial receipt.

The execution producer runs live inside the change context: `run_verifier`
takes the HEAD worktree and the HEAD graph as one fused pair (the affected
tests the head graph names must exist in the tree the spawn runs in; the
base-side dual belongs to the regression join). Its result is coerced
fail-closed into the receipt's execution lines — verify() itself raises only
on invalid arguments (a census bug, surfaced loudly), while every
operational failure arrives INSIDE the result as an honest abstention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from hive.census.diff import DiffError, current_ref, open_change
from hive.census.engines import (
    EngineError,
    attribute_symbols,
    build_graphs,
    ensure_scratch_matrix_out,
    node_change,
)
from hive.census.envelope import unsigned_envelope
from hive.census.execution import coerce_execution, coerce_verifier_stamp, run_verifier
from hive.census.join import regression_join
from hive.census.memory import fetch_institutional_context, order_subjects
from hive.census.propagation import propagation_block
from hive.census.receipt import build_receipt
from hive.census.schema import ReceiptSchemaError

EX_OK = 0
EX_SOFTWARE = 70


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hive-census",
        description="Compose an unsigned, fail-closed change receipt for one base..head change.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build", help="build the receipt envelope for a base..head pair"
    )
    build.add_argument("--repo", required=True, type=Path, help="git repository root")
    build.add_argument("--base", required=True, help="base ref (the before side)")
    build.add_argument("--head", required=True, help="head ref (the after side)")
    build.add_argument(
        "--out",
        type=Path,
        default=Path("receipt.json"),
        help="envelope output path (default: receipt.json)",
    )
    build.add_argument(
        "--depth", type=int, default=2, help="blast-radius depth (default: 2)"
    )
    build.add_argument(
        "--precision-budget",
        type=float,
        default=0.9,
        help="per-class decided-fraction gating budget (default: 0.9)",
    )
    build.add_argument(
        "--authoritative",
        action="store_true",
        help="run each affected config dir's full suite (sound, slower); default runs "
        "the scoped affected-test set and tags decided classes bounded-estimate",
    )
    build.add_argument(
        "--hive-url",
        default=None,
        help="loopback hive MCP door (e.g. http://127.0.0.1:8765/mcp); when set, "
        "touched subjects are recalled from the fleet memory and served lessons "
        "ride the receipt's context block as trust-labelled reference — an "
        "absent or unreachable hive changes nothing else in the receipt",
    )
    build.add_argument(
        "--memory-cap",
        type=int,
        default=10,
        help="max institutional-memory context entries (default: 10)",
    )
    build.add_argument(
        "--propagate",
        action="store_true",
        help="carry each breaking/removed seed's blast-radius neighbourhood "
        "(callers/dependents/tests, rendered as head-graph path::Symbol) on the "
        "receipt's propagation block, so an ingesting hive can mark co-anchored "
        "memories as stale suspects; default off — the receipt is byte-identical "
        "without it",
    )
    build.add_argument(
        "--ref",
        default=None,
        help="explicit measured-line override recorded verbatim as provenance.ref "
        "(e.g. refs/pull/7/head, for a builder measuring a detached candidate "
        "worktree); default: the checkout's current branch, as always",
    )
    build.add_argument(
        "--repo-id",
        default=None,
        help="repository identity recorded verbatim as provenance.repo; absent, "
        "the receipt is byte-identical to a build without the flag",
    )
    return parser


def _memory_subjects(verdict: object) -> list[tuple[str, str]]:
    """The recall order for the verdict's touched subjects: breaking/removed
    drift first, then lexicographic — highest stakes survive the cap."""
    return order_subjects(
        tuple(
            (
                str(getattr(change, "path", "") or ""),
                str(getattr(change, "symbol", "") or ""),
                str(getattr(change, "drift", "") or ""),
            )
            for change in getattr(verdict, "symbols", ()) or ()
        )
    )


def _write_atomic(out_path: Path, doc: dict) -> None:
    """Write the envelope via temp + rename: the out path is all or nothing."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, out_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    out_path = Path(args.out)
    try:
        ensure_scratch_matrix_out()
        # The measured line: the checkout's own branch, resolved here — unless the
        # builder names the line explicitly (--ref: a detached candidate worktree's
        # checkout is NOT the line being measured). None (detached/fault) ⇒ no stamp.
        ref = args.ref if args.ref else current_ref(Path(args.repo))
        with open_change(Path(args.repo), args.base, args.head) as change:
            graphs = build_graphs(change)
            touched_symbols = attribute_symbols(change.touched, graphs)
            verdict = node_change(change, touched_symbols)
            findings = regression_join(verdict, graphs.base, depth=args.depth)
            result = run_verifier(
                change, graphs, depth=args.depth, authoritative=args.authoritative
            )
            # Memory is a side-channel: the fetch itself fails open, so a
            # down or absent hive builds the identical receipt minus the
            # context block; without --hive-url the door is never touched.
            context: list = []
            if args.hive_url:
                context = fetch_institutional_context(
                    args.hive_url,
                    _memory_subjects(verdict),
                    cap_total=args.memory_cap,
                )
            # Propagation is opt-in: without the flag the block builder is never
            # touched and the receipt stays byte-identical.
            propagation: list = []
            if args.propagate:
                propagation = propagation_block(findings, graphs)
            receipt = build_receipt(
                touched=change.touched,
                verdict=verdict,
                findings=findings,
                execution=coerce_execution(result, authoritative=args.authoritative),
                verifier_stamp=coerce_verifier_stamp(result),
                graphs=graphs,
                precision_budget=args.precision_budget,
                depth=args.depth,
                context=context,
                propagation=propagation,
                ref=ref,
                repo=args.repo_id,
            )
        envelope = unsigned_envelope(receipt)
        _write_atomic(out_path, envelope)
    except (DiffError, EngineError, ReceiptSchemaError, OSError) as error:
        print(f"hive-census: {error}", file=sys.stderr)
        return EX_SOFTWARE

    print(f"wrote receipt envelope to {out_path}")
    if args.hive_url:
        print(f"  institutional-memory context: {len(context)} entries")
    if args.propagate:
        print(f"  propagation: {len(propagation)} seeds")
    for entry in receipt["precision"]:
        value = "null" if entry["value"] is None else format(entry["value"], ".3f")
        gating = "true" if entry["gating"] else "false"
        print(
            f"  {entry['class']}: decided {entry['decided']}/{entry['total']} "
            f"value={value} gating={gating}"
        )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
