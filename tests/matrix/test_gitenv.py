"""matrix.gitenv — the ONE owner of git's repo-discovery denylist (BUG-034).

Re-homed from hive_census (U1): matrix sits at the workspace's dependency
floor, so census and the edge CLI import this module instead of keeping
byte-identical twins pinned by a cross-copy test. An inherited (not
`-C`-derived) GIT_DIR silently overrides `git -C <path>` targeting inside a
git hook subprocess; stripping the seven vars is what keeps every child on
the intended repository.
"""

from __future__ import annotations

from hive.matrix import gitenv


def test_clean_git_env_strips_repo_discovery_vars(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
    monkeypatch.setenv("HIVE_GITENV_KEEP", "1")
    env = gitenv.clean_git_env()
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["HIVE_GITENV_KEEP"] == "1"  # everything else rides through


def test_clean_git_env_strips_every_named_discovery_var(monkeypatch):
    for var in gitenv._GIT_REPO_DISCOVERY_VARS:
        monkeypatch.setenv(var, "poison")
    env = gitenv.clean_git_env()
    assert not any(var in env for var in gitenv._GIT_REPO_DISCOVERY_VARS)


def test_version_stamp_uses_the_shared_owner():
    # The old twin-copy pin inverted: version.py now IMPORTS the one owner,
    # so identity (not byte-equality of two copies) is the contract.
    from hive.matrix import version

    assert version.clean_git_env is gitenv.clean_git_env
