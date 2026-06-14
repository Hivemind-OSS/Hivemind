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

# entrypoint-owned operator aliases (bridged into Config.load, NOT routed through HIVE_*__)
_ENTRYPOINT_OWNED = {"HIVE_TENANT_ID", "HIVE_AGENT_ID", "HIVE_STORE__DB_PATH"}
# pure process env (consumed by HF libs, not by Config) — not subject to the config map
_RUNTIME_ENV = {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"}


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


def _resolve_interp(value: str) -> str:
    """A compose ``${VAR:-default}`` interpolation resolves (host-side, var unset) to its
    default — return that so the key↔field coercion check sees the real pinned value, not
    the literal ``${...}`` expression. Non-interpolated values pass through unchanged."""
    m = re.fullmatch(r"\$\{[^:}]+:-(.*)\}", value)
    return m.group(1) if m else value


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


def _pinned_config_fields() -> set[str]:
    """Every compose env key that resolves to a real config (group, field), as canonical
    ``group.field`` — excludes the entrypoint-owned aliases (bridged separately)."""
    return {f for f in (_key_to_field(k) for k in _env_pairs()
                        if k not in _ENTRYPOINT_OWNED) if f}


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


def test_tenant_id_compose_level_failfast_literal():
    # ${HIVE_TENANT_ID:?...} ⇒ compose itself errors if the var is unset (no silent default)
    assert re.search(r"\$\{HIVE_TENANT_ID:\?", _text())


def test_named_volume_persists():
    assert re.search(r"(?m)^\s+hive-data:\s*$", _text())
    assert re.search(r"name:\s*hive-data", _text())


def test_compose_env_keys_resolve():
    """Every HIVE_* env key is either entrypoint-owned OR names a REAL (group, field) in
    Config and coerces — closing the dead-knob drift the verbatim keys had
    (HIVE_EMBEDDING__MODEL_NAME / HIVE_OBSERVABILITY__LOG_LEVEL named nothing)."""
    pairs = _env_pairs()
    hive_keys = [k for k in pairs if k.startswith("HIVE_")]
    assert hive_keys, "no HIVE_* env keys parsed from compose"
    unresolved = []
    for key in hive_keys:
        if key in _ENTRYPOINT_OWNED:
            continue
        body = key[len("HIVE_"):]
        group_tok, sep, field_tok = body.partition("__")
        if not sep:
            unresolved.append((key, "no __ separator and not entrypoint-owned"))
            continue
        group = group_tok.lower()
        if group not in C._GROUP_TYPES:
            unresolved.append((key, f"unknown group {group!r}"))
            continue
        typ = C._GROUP_TYPES[group]
        fld = {f.name.lower(): f for f in dataclasses.fields(typ)}.get(field_tok.lower())
        if fld is None:
            unresolved.append((key, f"unknown field {field_tok!r} on {group}"))
            continue
        try:
            C._coerce(_resolve_interp(pairs[key]), fld.type, fld.name)  # ${VAR:-default} → default
        except (ValueError, TypeError) as exc:
            unresolved.append((key, f"value {pairs[key]!r} not coercible: {exc}"))
    assert not unresolved, f"compose env keys that do not resolve to a live config field: {unresolved}"


def test_no_legacy_dead_keys():
    # the two keys that named nothing must not reappear
    pairs = _env_pairs()
    assert "HIVE_EMBEDDING__MODEL_NAME" not in pairs
    assert "HIVE_OBSERVABILITY__LOG_LEVEL" not in pairs


# ── the guarantee firewall (agent-config-tuning) ──────────────────────────────
def test_guarantee_knobs_are_env_pinned():
    # the four safety guarantees MUST be pinned at the env layer (3) so the agent's TOML
    # (layer 2) cannot loosen them. SSOT is config.GUARANTEE_KNOBS — drift either way reds.
    pinned = _pinned_config_fields()
    missing = set(C.GUARANTEE_KNOBS) - pinned
    assert not missing, f"guarantee knobs not env-pinned in compose: {sorted(missing)}"


def test_no_agent_knob_is_env_pinned():
    # pinning an AGENT-authority knob would silently override the agent's TOML edits and
    # break the tuning loop — the firewall must freeze ONLY operator knobs.
    agent_pinned = {f for f in _pinned_config_fields()
                    if C.CONFIG_AUTHORITY.get(f) == "agent"}
    assert not agent_pinned, \
        f"agent knobs must NOT be env-pinned (defeats tuning): {sorted(agent_pinned)}"


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


@_SKIP
def test_tenant_id_required_fails_fast():
    env = {k: v for k, v in os.environ.items() if k != "HIVE_TENANT_ID"}
    r = subprocess.run(["docker", "compose", "-f", str(_COMPOSE), "config"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0
    assert "HIVE_TENANT_ID" in (r.stderr + r.stdout)
