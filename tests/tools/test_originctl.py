"""HIVE-ORIGIN — originctl: the stateless GitHub-API credit scanner (host half)
+ the settled-credit ingest (container half).

Every scan test drives ``scan_github`` through a canned exact-URL ``fetch`` seam —
zero network; an unrouted probe fails LOUD, which doubles as the pagination-stop
assertion (a dropped cutoff-stop would probe page 2 and red the test). ★ contracts:
merged-PR wins keyed ``merge_commit_sha`` with trailers harvested from the PR's
constituent commits (squash-safe by construction — the branch can be deleted);
direct-push wins; rebase twins dropped by TRACE-claim dedup (sha set-difference
cannot see rewritten shas); reverts → loss rows; strict trailer grammar (32-hex +
positive ints, commas tolerated) with malformed lines counted, never guessed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hive.tools import originctl

NOW = 1_760_000_000
DAY = 86_400
CUTOFF = NOW - originctl.DEFAULT_LOOKBACK_DAYS * DAY
TRACE_A = "a" * 32
TRACE_B = "b" * 32
TRACE_C = "c" * 32


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pulls_url(page: int = 1) -> str:
    return (f"{originctl.API_BASE}/repos/o/r/pulls"
            f"?state=closed&sort=updated&direction=desc"
            f"&per_page={originctl.PER_PAGE}&page={page}")


def pr_commits_url(number: int, page: int = 1) -> str:
    return (f"{originctl.API_BASE}/repos/o/r/pulls/{number}/commits"
            f"?per_page={originctl.PER_PAGE}&page={page}")


def commits_url(cutoff: int = CUTOFF, page: int = 1) -> str:
    return (f"{originctl.API_BASE}/repos/o/r/commits"
            f"?since={iso(cutoff)}&per_page={originctl.PER_PAGE}&page={page}")


class FakeFetch:
    """Exact-URL canned routes. A probe of an unrouted URL fails the test LOUD —
    that absence-assertion IS the pagination/cutoff-stop pin."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers):
        self.urls.append(url)
        self.headers.append(dict(headers))
        if url not in self.routes:
            raise AssertionError(
                f"unrouted fetch: {url}\nknown: {sorted(self.routes)}")
        payload = self.routes[url]
        status = 200
        if isinstance(payload, tuple):
            status, payload = payload
        return status, {}, json.dumps(payload).encode()


def _pr(number, *, merged_at=None, updated_at=None, merge_sha=None):
    return {"number": number,
            "merged_at": iso(merged_at) if merged_at else None,
            "updated_at": iso(updated_at if updated_at is not None
                              else (merged_at or NOW)),
            "merge_commit_sha": merge_sha or ("%040x" % number)}


def _commit(sha, message, ts):
    return {"sha": sha,
            "commit": {"message": message, "committer": {"date": iso(ts)}}}


def _scan(routes, **kw):
    fetch = FakeFetch(routes)
    rows, summary = originctl.scan_github("o/r", token=kw.pop("token", None),
                                          now=NOW, fetch=fetch, **kw)
    return rows, summary, fetch


def base_routes(**over):
    routes = {pulls_url(): [], commits_url(): []}
    routes.update(over)
    return routes


# ── parse_repo_arg: URL forms ───────────────────────────────────────────────────


def test_parse_repo_arg_url_forms():
    for raw in ("o/r", "https://github.com/o/r", "https://github.com/o/r/",
                "https://github.com/o/r.git", "git@github.com:o/r.git",
                "github.com/o/r"):
        assert originctl.parse_repo_arg(raw) == "o/r"


def test_parse_repo_arg_rejects_non_github_and_malformed():
    for bad in ("", "o", "o/r/extra", "https://gitlab.com/o/r",
                "git@gitlab.com:o/r.git", "o//r"):
        with pytest.raises(ValueError):
            originctl.parse_repo_arg(bad)


# ── parse_trailer_lines: the strict v2 grammar ──────────────────────────────────


def test_parse_trailer_lines_strict_grammar_and_malformed_count():
    body = (f"feat X\n\n"
            f"Hive-Credit: {TRACE_A} 12, 34\n"        # commas tolerated
            f"hive-credit: {TRACE_B} 7\n"             # key case-insensitive (git semantics)
            f"Hive-Credit: {TRACE_C.upper()} 9\n"     # hex case-normalized
            f"Hive-Credit: nothex 5\n"                # bad trace → malformed
            f"Hive-Credit: {'d' * 31} 5\n"            # 31-hex → malformed
            f"Hive-Credit: {TRACE_C} 0\n"             # 0 not positive → malformed
            f"Hive-Credit: {TRACE_C} -3\n"            # negative → malformed
            f"Hive-Credit: {TRACE_C}\n")              # no episode ids → malformed
    groups, malformed = originctl.parse_trailer_lines(body, trailer_key="Hive-Credit")
    assert groups == [(TRACE_A, [12, 34]), (TRACE_B, [7]), (TRACE_C, [9])]
    assert malformed == 5


def test_parse_trailer_lines_no_trailers():
    assert originctl.parse_trailer_lines("plain message",
                                         trailer_key="Hive-Credit") == ([], 0)


# ── scan: merged PRs (primary win source) ───────────────────────────────────────


def test_merged_pr_win_keyed_merge_sha_constituent_trailers_squash_safe():
    # The squash scenario: the feature BRANCH is deleted after merge — trailers are
    # still served by /pulls/{n}/commits (GitHub keeps refs/pull/N/head), so the
    # harvest needs no repo-settings dance. Win keyed the MERGE sha, ts merged_at.
    merged_ts = NOW - 2 * DAY
    routes = {
        pulls_url(): [_pr(7, merged_at=merged_ts, merge_sha="f" * 40)],
        pr_commits_url(7): [
            _commit("1" * 40, f"part 1\n\nHive-Credit: {TRACE_A} 12 34", merged_ts - 100),
            _commit("2" * 40, f"part 2\n\nHive-Credit: {TRACE_B} 56", merged_ts - 50),
        ],
        commits_url(): [],
    }
    rows, summary, _ = _scan(routes)
    assert rows == [{"kind": "win", "commit_sha": "f" * 40, "commit_ts": merged_ts,
                     "repo": "o/r",
                     "credits": [{"trace_id": TRACE_A, "episode_ids": [12, 34]},
                                 {"trace_id": TRACE_B, "episode_ids": [56]}]}]
    assert summary["win_rows"] == 1 and summary["prs_scanned"] == 1
    assert summary["credit_groups"] == 2


def test_pr_pagination_stops_at_cutoff():
    # Page 1 is FULL (would otherwise paginate) and its second item is pre-cutoff:
    # the updated-desc sort means everything after is older, so page 2 must never
    # be probed (FakeFetch reds the test on any unrouted URL).
    old = CUTOFF - 5 * DAY
    fresh = NOW - 1 * DAY
    page1 = [_pr(1, merged_at=fresh, merge_sha="e" * 40)] + [
        _pr(100 + i, merged_at=old, updated_at=old)
        for i in range(originctl.PER_PAGE - 1)]
    routes = {
        pulls_url(1): page1,
        pr_commits_url(1): [_commit("1" * 40, f"x\n\nHive-Credit: {TRACE_A} 5", fresh)],
        commits_url(): [],
    }
    rows, summary, fetch = _scan(routes)
    assert summary["prs_scanned"] == 1 and summary["win_rows"] == 1
    assert not any("page=2" in u for u in fetch.urls)


def test_closed_unmerged_and_premerged_prs_skipped():
    routes = {
        pulls_url(): [
            _pr(1, updated_at=NOW - DAY),                            # closed, never merged
            _pr(2, merged_at=CUTOFF - DAY, updated_at=NOW - DAY),    # merged pre-window
        ],
        commits_url(): [],
    }
    rows, summary, fetch = _scan(routes)
    assert rows == [] and summary["prs_scanned"] == 0
    assert not any("/pulls/1/commits" in u or "/pulls/2/commits" in u
                   for u in fetch.urls)


def test_pr_without_trailers_emits_no_row():
    merged_ts = NOW - DAY
    routes = {
        pulls_url(): [_pr(3, merged_at=merged_ts)],
        pr_commits_url(3): [_commit("9" * 40, "no trailers here", merged_ts)],
        commits_url(): [],
    }
    rows, summary, _ = _scan(routes)
    assert rows == [] and summary["prs_scanned"] == 1 and summary["win_rows"] == 0


# ── scan: direct pushes + rebase-twin dedup + losses ────────────────────────────


def test_direct_push_trailer_commit_becomes_win_keyed_own_sha():
    ts = NOW - 3 * DAY
    routes = base_routes(**{commits_url(): [
        _commit("d" * 40, f"hotfix\n\nHive-Credit: {TRACE_A} 5", ts)]})
    rows, summary, _ = _scan(routes)
    assert rows == [{"kind": "win", "commit_sha": "d" * 40, "commit_ts": ts,
                     "repo": "o/r",
                     "credits": [{"trace_id": TRACE_A, "episode_ids": [5]}]}]
    assert summary["win_rows"] == 1 and summary["deduped_rebase_copies"] == 0


def test_rebase_twin_dropped_by_trace_claim_dedup():
    # A rebase-merge rewrites the constituent shas, so the branch listing shows the
    # SAME trailer under a NEW sha — only the trace ids are a stable join. The twin
    # is suppressed and counted; an unrelated direct push still lands.
    merged_ts = NOW - 2 * DAY
    routes = {
        pulls_url(): [_pr(7, merged_at=merged_ts, merge_sha="e" * 40)],
        pr_commits_url(7): [
            _commit("1" * 40, f"x\n\nHive-Credit: {TRACE_A} 5", merged_ts)],
        commits_url(): [
            _commit("9" * 40, f"x\n\nHive-Credit: {TRACE_A} 5", merged_ts),
            _commit("8" * 40, f"y\n\nHive-Credit: {TRACE_B} 6", merged_ts),
        ],
    }
    rows, summary, _ = _scan(routes)
    wins = [r for r in rows if r["kind"] == "win"]
    assert {w["commit_sha"] for w in wins} == {"e" * 40, "8" * 40}
    assert summary["deduped_rebase_copies"] == 1
    assert summary["win_rows"] == 2


def test_revert_commit_emits_loss_row_per_named_sha():
    ts = NOW - DAY
    target = "abc" + "0" * 37
    routes = base_routes(**{commits_url(): [
        _commit("f" * 40, f'Revert "feat"\n\nThis reverts commit {target}.', ts)]})
    rows, summary, _ = _scan(routes)
    assert rows == [{"kind": "loss", "commit_sha": target, "commit_ts": ts,
                     "repo": "o/r"}]
    assert summary["loss_rows"] == 1 and summary["win_rows"] == 0


def test_legacy_trailer_sightings_counted_inert():
    # v1 Hive-Trace trailers earn NOTHING (v1 promised "changes no reward") but are
    # counted every sync — the re-onboard nudge. Sightings in PR constituents and
    # the branch listing both count.
    merged_ts = NOW - DAY
    routes = {
        pulls_url(): [_pr(4, merged_at=merged_ts)],
        pr_commits_url(4): [_commit("1" * 40, "old\n\nHive-Trace: tA tB", merged_ts)],
        commits_url(): [_commit("2" * 40, "old2\n\nhive-trace: tC", merged_ts)],
    }
    rows, summary, _ = _scan(routes)
    assert rows == []                                   # inert: no win, no credit
    assert summary["legacy_trailers_seen"] == 2


def test_malformed_trailer_lines_counted_never_guessed():
    ts = NOW - DAY
    routes = base_routes(**{commits_url(): [
        _commit("d" * 40, f"x\n\nHive-Credit: {TRACE_A} twelve", ts)]})
    rows, summary, _ = _scan(routes)
    assert rows == []
    assert summary["malformed_trailer_lines"] == 1


def test_lookback_days_threads_into_both_windows():
    cutoff90 = NOW - 90 * DAY
    old_merge = NOW - 30 * DAY                          # outside 14d, inside 90d
    routes = {
        pulls_url(): [_pr(5, merged_at=old_merge, merge_sha="c" * 40)],
        pr_commits_url(5): [
            _commit("3" * 40, f"z\n\nHive-Credit: {TRACE_C} 8", old_merge)],
        commits_url(cutoff90): [],
    }
    rows, summary, _ = _scan(routes, lookback_days=90)
    assert summary["win_rows"] == 1                     # 30d-old merge inside 90d window


def test_commits_listing_paginates_full_pages():
    ts = NOW - DAY
    page1 = [_commit("%040x" % i, "noise", ts) for i in range(originctl.PER_PAGE)]
    routes = base_routes(**{
        commits_url(): page1,
        commits_url(page=2): [
            _commit("d" * 40, f"x\n\nHive-Credit: {TRACE_A} 5", ts)],
    })
    rows, summary, _ = _scan(routes)
    assert summary["win_rows"] == 1                     # the page-2 win was reached


def test_token_rides_authorization_header_and_absent_without():
    _, _, fetch = _scan(base_routes(), token="ghp_secret")
    assert all(h.get("Authorization") == "Bearer ghp_secret" for h in fetch.headers)
    _, _, fetch2 = _scan(base_routes(), token=None)
    assert all("Authorization" not in h for h in fetch2.headers)


def test_scan_raises_loud_on_api_error_status():
    routes = {pulls_url(): (404, {"message": "Not Found"})}
    with pytest.raises(originctl.GithubApiError) as ei:
        _scan(routes)
    assert ei.value.status == 404


# ── ingest: claimed ∩ served, idempotent, monotone under rescans ────────────────


@pytest.fixture()
def store():
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore
    return SqliteEpisodeStore(connect(":memory:"))


def _win(sha="a1", traces=((TRACE_A, (3, 4)),), ts=100):
    return {"kind": "win", "commit_sha": sha, "commit_ts": ts, "repo": "o/r",
            "credits": [{"trace_id": t, "episode_ids": list(e)} for t, e in traces]}


def _loss(sha="a1", ts=110):
    return {"kind": "loss", "commit_sha": sha, "commit_ts": ts, "repo": "o/r"}


def test_ingest_credits_claimed_intersect_served_with_real_margin(store):
    store.record_exposure(TRACE_A, [(3, 0.9), (4, 0.2)], agent_id="a", ts=50)
    # claims 3 (served on this trace) + 99 (never served) — only the intersection lands
    report = originctl.ingest(store, [_win(traces=((TRACE_A, (3, 99)),))], now=200)
    assert report["wins_ingested"] == 1
    assert report["unserved_claims"] == 1
    rows = store.conn.execute(
        "SELECT episode_id, outcome, recall_margin, repo FROM task_outcomes").fetchall()
    assert [(r["episode_id"], r["outcome"]) for r in rows] == [(3, "win")]
    assert rows[0]["recall_margin"] == 0.9        # the REAL margin, from the exposure row
    assert rows[0]["repo"] == "o/r"


def test_ingest_unknown_trace_counted_not_written(store):
    report = originctl.ingest(store, [_win(traces=((TRACE_B, (5,)),))], now=200)
    assert report["unknown_traces"] == 1 and report["wins_ingested"] == 0
    n = store.conn.execute("SELECT COUNT(*) AS c FROM task_outcomes").fetchone()["c"]
    assert n == 0


def test_ingest_idempotent_reingest_counts_duplicates(store):
    store.record_exposure(TRACE_A, [(3, 0.9), (4, 0.2)], agent_id="a", ts=50)
    r1 = originctl.ingest(store, [_win()], now=200)
    assert r1["wins_ingested"] == 2 and r1["duplicates"] == 0
    r2 = originctl.ingest(store, [_win()], now=300)            # hourly re-scan, same rows
    assert r2["wins_ingested"] == 0 and r2["duplicates"] == 2
    n = store.conn.execute("SELECT COUNT(*) AS c FROM task_outcomes").fetchone()["c"]
    assert n == 2


def test_merge_and_revert_in_one_batch_wins_land_then_flip(store):
    store.record_exposure(TRACE_A, [(3, 0.9)], agent_id="a", ts=50)
    # loss FIRST in row order: ingest must process wins before losses regardless
    report = originctl.ingest(store, [_loss("a1"), _win("a1", traces=((TRACE_A, (3,)),))],
                              now=200)
    assert report["wins_ingested"] == 1 and report["losses_flipped"] == 1
    row = store.conn.execute("SELECT outcome FROM task_outcomes").fetchone()
    assert row["outcome"] == "loss"


def test_monotone_win_then_loss_then_reingested_win_stays_loss(store):
    # the CV6-gap pin: a revert arriving AFTER the win's ingest settles it loss; the
    # next stateless re-scan re-offering the same win cannot resurrect it.
    store.record_exposure(TRACE_A, [(3, 0.9)], agent_id="a", ts=50)
    originctl.ingest(store, [_win("a1", traces=((TRACE_A, (3,)),))], now=200)
    r2 = originctl.ingest(store, [_loss("a1")], now=300)
    assert r2["losses_flipped"] == 1
    r3 = originctl.ingest(store, [_win("a1", traces=((TRACE_A, (3,)),))], now=400)
    assert r3["wins_ingested"] == 0 and r3["duplicates"] == 1
    row = store.conn.execute("SELECT outcome FROM task_outcomes").fetchone()
    assert row["outcome"] == "loss"


def test_loss_for_uncredited_sha_flips_nothing(store):
    report = originctl.ingest(store, [_loss("nothere")], now=200)
    assert report["losses_flipped"] == 0
    assert report["totals"] == {"settled_rows": 0, "distinct_episodes": 0}


# ── the module entry (`python -m hive.tools.originctl ingest --ndjson -`) ───────


def test_main_ingest_reads_ndjson_stdin_reports_json(tmp_path):
    import io
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore
    db = str(tmp_path / "shared.db")
    SqliteEpisodeStore(connect(db)).record_exposure(
        TRACE_A, [(3, 0.9)], agent_id="a", ts=50)
    ndjson = json.dumps(_win(traces=((TRACE_A, (3,)),))) + "\n"
    out = io.StringIO()
    rc = originctl.main(["ingest", "--ndjson", "-"],
                        env={"HIVE_STORE__DB_PATH": db},
                        stdin=io.StringIO(ndjson), out=out)
    assert rc == originctl.EX_OK
    report = json.loads(out.getvalue())
    assert report["wins_ingested"] == 1
    assert report["totals"]["settled_rows"] == 1


def test_main_missing_db_is_config_error():
    import io
    rc = originctl.main(["ingest", "--ndjson", "-"], env={},
                        stdin=io.StringIO(""), out=io.StringIO())
    assert rc == originctl.EX_CONFIG


def test_main_bad_ndjson_is_software_error():
    import io
    rc = originctl.main(["ingest", "--ndjson", "-"],
                        env={"HIVE_STORE__DB_PATH": ":memory:"},
                        stdin=io.StringIO("{not json\n"), out=io.StringIO())
    assert rc == originctl.EX_SOFTWARE
