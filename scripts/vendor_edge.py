"""Refresh the committed vendor/wheels/ wheelhouse from the local hive-edge workspace.

The server host receives ONLY the hivemind repo — no package index, no sibling
hive-edge checkout. The image build therefore installs the census/sync engines
(comb-drift, matrix) and the in-image `hive-edge` mint CLI from the wheelhouse
committed at vendor/wheels/. This script is the single seam that touches the
hive-edge workspace: run it on a dev machine whenever hive-edge changes, then
commit the refreshed wheels alongside any hivemind change that depends on them.

Post-conditions enforced before it exits 0:
  * exactly the three expected distributions are present (one wheel each);
  * the three wheel versions are IDENTICAL — the hive-edge workspace ships ONE
    lockstep version across all its distributions, so a half-bumped workspace
    is refused here before it can reach the committed wheelhouse;
  * the hive_edge wheel's version satisfies the MIN_EDGE_VERSION floor the
    server serves to agents (hive/app/onboard_ref.py) — the server's own mint
    tooling may never lag the floor it tells the fleet to meet;
  * the [tool.uv.sources] wheel-path pins in pyproject.toml are rewritten to
    the freshly built filenames and `uv lock` re-resolves — wheelhouse,
    pyproject, and lockfile leave this script coherent as one unit.

Usage: python scripts/vendor_edge.py [--edge PATH]   (default: ../hive-edge)
Dependencies: stdlib + the `uv` binary on PATH (build backend runner).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WHEELHOUSE = REPO / "vendor" / "wheels"
ONBOARD_REF = REPO / "hive" / "app" / "onboard_ref.py"

# workspace-relative source dirs, keyed by the wheel's normalized dist prefix
MEMBERS = {
    "comb_drift": "packages/comb-drift",
    "matrix": "packages/matrix",
    "hive_edge": ".",
}


def _die(msg: str) -> "NoReturn":  # noqa: F821 — py<3.11 spelling unneeded; runtime is 3.12
    sys.exit(f"vendor_edge: {msg}")


def _version_tuple(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        _die(f"non-release version {version!r} — vendor only clean X.Y.Z builds")
    return tuple(int(p) for p in version.split("."))


def _min_edge_version() -> tuple[int, ...]:
    m = re.search(r'MIN_EDGE_VERSION:\s*str\s*=\s*"([^"]+)"', ONBOARD_REF.read_text())
    if m is None:
        _die(f"MIN_EDGE_VERSION not found in {ONBOARD_REF}")
    return _version_tuple(m.group(1))


def _repin_uv_sources(wheels: dict[str, str]) -> None:
    """Rewrite the [tool.uv.sources] wheel-path pins to the freshly built filenames.

    The pins name exact wheel files (a flat index would serialize machine-absolute
    file:// URLs into the committed uv.lock), so a version bump must land here in the
    same refresh. Loud on any missing pin line — a silently un-repinned name would
    resolve dev and image to different engine versions.
    """
    pyproject = REPO / "pyproject.toml"
    text = pyproject.read_text()
    for dist, source_name in (("hive_edge", "hive-edge"),
                              ("comb_drift", "comb-drift"),
                              ("matrix", "matrix")):
        pattern = rf'(?m)^{re.escape(source_name)} = \{{ path = "vendor/wheels/[^"]+" \}}$'
        line = f'{source_name} = {{ path = "vendor/wheels/{dist}-{wheels[dist]}-py3-none-any.whl" }}'
        text, n = re.subn(pattern, line, text)
        if n != 1:
            _die(f"expected exactly one [tool.uv.sources] wheel pin for {source_name!r} "
                 f"in pyproject.toml, found {n} — restore the pin line, then re-run")
    pyproject.write_text(text)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--edge", default=str(REPO.parent / "hive-edge"),
                        help="path to the hive-edge workspace checkout (default: ../hive-edge)")
    args = parser.parse_args(argv)

    edge_root = Path(args.edge).resolve()
    if not (edge_root / "pyproject.toml").is_file():
        _die(f"no hive-edge workspace at {edge_root} (pass --edge)")

    WHEELHOUSE.mkdir(parents=True, exist_ok=True)
    for stale in WHEELHOUSE.glob("*.whl"):
        stale.unlink()

    for source in MEMBERS.values():
        subprocess.run(
            ["uv", "build", "--wheel", str(edge_root / source), "--out-dir", str(WHEELHOUSE)],
            check=True,
        )

    # uv drops a `.gitignore` (containing `*`) into --out-dir; committed wheels are the point
    (WHEELHOUSE / ".gitignore").unlink(missing_ok=True)

    wheels = sorted(WHEELHOUSE.glob("*.whl"))
    built = {w.name.split("-")[0]: w.name.split("-")[1] for w in wheels}
    if set(built) != set(MEMBERS) or len(wheels) != len(MEMBERS):
        _die(f"expected exactly one wheel per {sorted(MEMBERS)}, got {sorted(w.name for w in wheels)}")

    if len(set(built.values())) != 1:
        _die("workspace version drift in the built wheels: "
             + ", ".join(f"{dist} {version}" for dist, version in sorted(built.items()))
             + " — the hive-edge workspace ships ONE lockstep version; "
             "bump every member to the same version, then re-run")

    floor = _min_edge_version()
    if _version_tuple(built["hive_edge"]) < floor:
        _die(f"hive_edge wheel {built['hive_edge']} is below the served MIN_EDGE_VERSION "
             f"floor {'.'.join(map(str, floor))} — bump hive-edge (or the floor) first")

    _repin_uv_sources(built)
    subprocess.run(["uv", "lock"], cwd=REPO, check=True)

    for name, version in sorted(built.items()):
        print(f"vendored {name} {version}")
    print(f"wheelhouse refreshed: {WHEELHOUSE.relative_to(REPO)} — commit the wheels, "
          "pyproject.toml, and uv.lock together")


if __name__ == "__main__":
    main()
