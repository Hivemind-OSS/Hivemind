"""P1.13 / M12 — the compose contract.

Two layers: (1) daemon-free TEXT assertions on the long-lived-warm-server transport
decision (restart: unless-stopped, stdin attach — NOT `run --rm` cold-warm-per-restart),
the compose-level tenant fail-fast, and the env-key↔config alignment (the corrected
HIVE_EMBEDDING__MODEL / HIVE_OBS__LOG_LEVEL keys must name a REAL config field, else they
are dead operator knobs); (2) `docker compose config` validity, skip-guarded on the CLI.

Part A adds the tunnel contract: the `ngrok` sidecar is PROFILE-GATED
(a plain `up` starts no tunnel — AC2), publishes no host port (egress only), reaches the
daemon over the COMPOSE network (`hive-server:8765`, never a host address), only tunnels
a HEALTHY daemon, and lives in its own image — the hive image bakes in no tunnel binary
(D1/AC3, the hermetically-offline invariant). The deliberate mutation (drop
`profiles: ["tunnel"]` → a default `up` would expose the daemon) reds
`test_tunnel_is_profile_gated`.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hive.app import config as C

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "compose.yaml"


def _text() -> str:
    return _COMPOSE.read_text()


def _env_pairs() -> dict[str, str]:
    """Extract the service `environment:` KEY: "VALUE" pairs (6-space-indented, UPPER keys).
    Strips a double-quoted value's quotes and any trailing inline `# comment`."""
    out: dict[str, str] = {}
    for line in _text().splitlines():
        m = re.match(r'^\s+([A-Z][A-Z0-9_]+):\s*(.+?)\s*$', line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw.startswith('"'):
            val = raw[1:].split('"', 1)[0]          # content between the first pair of quotes
        else:
            val = raw.split("#", 1)[0].strip()      # unquoted token, drop inline comment
        out[key] = val
    return out


def _key_to_field(key: str) -> str | None:
    """``HIVE_<GROUP>__<FIELD>`` → canonical ``"group.field"`` (real field-name case), or
    None if it is not a (group, field) env key. Mirrors the loader's case-fold matching."""
    if not key.startswith("HIVE_"):
        return None
    group_tok, sep, field_tok = key[len("HIVE_"):].partition("__")
    if not sep or group_tok.lower() not in C._GROUP_TYPES:
        return None
    fld = {f.name.lower(): f
           for f in dataclasses.fields(C._GROUP_TYPES[group_tok.lower()])}.get(field_tok.lower())
    return f"{group_tok.lower()}.{fld.name}" if fld else None


def test_compose_exists():
    assert _COMPOSE.is_file()


def test_long_lived_warm_server_not_run_rm():
    # transport decision: a long-lived warm daemon serving MCP over HTTP on a host-LOOPBACK
    # port, restarted by docker — NOT `docker compose run --rm` (which re-warms the embedder
    # per connection and defeats the no-network/cost model). The evidence is the loopback port
    # map + restart policy; the stdio-attach `stdin_open` belonged to the superseded transport.
    assert "restart: unless-stopped" in _text()
    assert "127.0.0.1:8765:8765" in _text()        # host-loopback HTTP mapping
    assert "run --rm" not in _text()


def test_http_port_is_loopback_only():
    # the daemon is reachable on host-loopback ONLY — never bound to a routable host interface.
    # A bare "8765:8765" (all-interfaces publish) is the regression this guards against.
    txt = _text()
    assert "127.0.0.1:8765:8765" in txt
    assert not re.search(r'(?m)^\s*-\s*"?8765:8765"?\s*$', txt)   # no all-interfaces publish


def test_compose_healthcheck_runs_healthcheck_module():
    assert re.search(r'test:\s*\["CMD",\s*"python",\s*"-m",\s*"hive\.tools\.healthcheck"\]', _text())


def test_named_volume_persists():
    assert re.search(r"(?m)^\s+hive-data:\s*$", _text())
    assert re.search(r"name:\s*hive-data", _text())


def test_compose_uses_env_file():
    # the single startup-config source: operator/startup knobs come from .env via env_file,
    # not inline compose keys — so a knob is never stated in two files.
    txt = _text()
    assert re.search(r"env_file:", txt)
    assert re.search(r"path:\s*\.env", txt)


def test_compose_carries_no_config_env_keys():
    # the no-duplication contract at the compose layer: NO HIVE_<group>__<field> config knob
    # appears in compose — they all live in .env. compose's `environment:` holds only image
    # invariants (the HF offline flags). A config key creeping back in reds here.
    config_keys = sorted(k for k in _env_pairs() if _key_to_field(k) is not None)
    assert not config_keys, f"compose must carry no config knobs (move to .env): {config_keys}"


def test_no_legacy_dead_keys():
    # the two keys that named nothing must not reappear
    pairs = _env_pairs()
    assert "HIVE_EMBEDDING__MODEL_NAME" not in pairs
    assert "HIVE_OBSERVABILITY__LOG_LEVEL" not in pairs


# ── agent-config-tuning: the tunable file ─────────────────────────────────────
def test_config_toml_is_mounted_and_present():
    # the agent-tunable layer: a git-tracked file bind-mounted read-only at the path the
    # entrypoint's Config.load reads by default (/data/hive.toml). Shipping it git-tracked
    # is what stops Docker turning a missing source into a silent directory.
    assert re.search(r"\./hive\.config\.toml:/data/hive\.toml:ro", _text()), \
        "compose must bind-mount ./hive.config.toml:/data/hive.toml:ro"
    assert (_ROOT / "hive.config.toml").is_file(), "hive.config.toml must exist at repo root"


# ── the opt-in public tunnel (Part A) ─────────────────────────────────────────
def _service_block(name: str) -> str:
    """The literal text of one 2-space-indented service block: from `  <name>:` until the
    next line at ≤2-space indentation (sibling service, top-level key, or 2-indent comment)."""
    lines, out, capturing = _text().splitlines(), [], False
    for ln in lines:
        if re.match(rf"^  {re.escape(name)}:\s*$", ln):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            if ln.strip() and re.match(r"^(\S|  \S)", ln):
                break
            out.append(ln)
    return "\n".join(out)


def test_tunnel_is_profile_gated():
    # AC2: a plain `docker compose up` must NOT start ngrok — exposure is the explicit
    # `--profile tunnel`. No host `ports:` either: the sidecar is egress-only.
    blk = _service_block("ngrok")
    assert blk, "ngrok service missing from compose.yaml"
    assert re.search(r'profiles:\s*\[\s*"tunnel"\s*\]', blk)   # ← deliberate mutation: removing this reds here
    assert "ports:" not in blk


def test_tunnel_depends_on_healthy_daemon():
    # only tunnel a daemon that is actually warm (healthcheck = embedder-resident gate)
    blk = _service_block("ngrok")
    assert "depends_on" in blk
    assert "hive-server" in blk and "service_healthy" in blk


def test_tunnel_uses_compose_network_upstream():
    # the sidecar reaches the daemon over the COMPOSE network — zero host-exposure change
    # (D5: the host publish stays loopback-only; no LAN bind, no host.docker.internal).
    blk = _service_block("ngrok")
    assert "hive-server:8765" in blk
    assert "127.0.0.1" not in blk and "host.docker.internal" not in blk


def test_hive_image_has_no_tunnel_baked_in():
    # D1/AC3: ngrok lives in its OWN image; the hive image stays hermetically offline —
    # no phone-home binary in the brain image, byte-identical to the pre-tunnel build.
    blk = _service_block("ngrok")
    m = re.search(r"image:\s*(\S+)", blk)
    assert m and m.group(1).startswith("ngrok/ngrok")
    assert "hive:vmin" not in blk
    dockerfile = (_ROOT / "Dockerfile").read_text()
    assert "ngrok" not in dockerfile.lower()


def test_env_example_documents_tunnel_vars():
    # the operator front door: tenant id live, tunnel credentials present but COMMENTED —
    # the example must never ship a real value (secrets stay out of git).
    env_ex = (_ROOT / ".env.example").read_text()
    assert "HIVE_TENANT_ID" in env_ex
    assert "NGROK_AUTHTOKEN" in env_ex and "NGROK_DOMAIN" in env_ex
    for ln in env_ex.splitlines():
        if "NGROK_AUTHTOKEN" in ln:
            assert ln.lstrip().startswith("#"), "NGROK_AUTHTOKEN must ship commented out"


# ── docker compose config validity (skip-guarded on the CLI; daemon-free) ─────
def _have_compose() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_SKIP = pytest.mark.skipif(not _have_compose(), reason="docker compose CLI not available")


@_SKIP
def test_compose_config_valid():
    env = dict(os.environ, HIVE_TENANT_ID="probe-tenant")
    r = subprocess.run(["docker", "compose", "-f", str(_COMPOSE), "config"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, f"compose config failed: {r.stderr}"
