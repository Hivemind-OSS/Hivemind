"""cluster_anomaly — the pure, flag-only embedding-cluster anomaly detector (3c).

It fires iff a candidate sits in an abnormally COMPACT near-dup cluster (the MINJA /
AgentPoison flood signature). Precision-first: a clean background stays quiet, and an
undecidable neighbor is skipped, never counted. It is DETECTION-ONLY — the not-blocked
integration pin lives in test_demand_rule.py."""

from __future__ import annotations

import math

import numpy as np

from hive.domain.anomaly import cluster_anomaly


def _at_cos(c: float) -> np.ndarray:
    """A unit vector whose cosine to e0 is ≈ c."""
    return np.array([c, math.sqrt(max(0.0, 1.0 - c * c)), 0.0, 0.0], dtype=np.float32)


E0 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


def test_compact_cluster_fires():
    # five near-identical neighbors within cos >= 0.95 ⇒ the flood signature fires
    neighbors = [_at_cos(0.99) for _ in range(5)]
    assert cluster_anomaly(E0, neighbors, tau=0.95, min_cluster=5) is True


def test_clean_background_stays_quiet():
    # diverse, well-separated neighbors (cos ~0.3) ⇒ no compact cluster ⇒ quiet
    neighbors = [_at_cos(0.3), _at_cos(0.2), _at_cos(0.1), _at_cos(0.25), _at_cos(0.15)]
    assert cluster_anomaly(E0, neighbors, tau=0.95, min_cluster=5) is False


def test_below_quorum_does_not_fire():
    # four close neighbors but min_cluster is 5 ⇒ one short ⇒ no flag (precision-first)
    neighbors = [_at_cos(0.99) for _ in range(4)]
    assert cluster_anomaly(E0, neighbors, tau=0.95, min_cluster=5) is False


def test_undecidable_neighbors_are_skipped():
    # a wrong-shape neighbor is undecidable ⇒ skipped, never counted toward the cluster
    bad = np.array([1.0, 0.0], dtype=np.float32)  # shape mismatch
    neighbors = [_at_cos(0.99), bad, _at_cos(0.99)]
    assert (
        cluster_anomaly(E0, neighbors, tau=0.95, min_cluster=3) is False
    )  # only 2 decidable
    assert cluster_anomaly(E0, neighbors, tau=0.95, min_cluster=2) is True


def test_empty_and_meaningless_threshold():
    assert cluster_anomaly(E0, [], tau=0.95, min_cluster=5) is False
    # a min_cluster below 1 is a meaningless threshold ⇒ never flags (defensive)
    assert cluster_anomaly(E0, [_at_cos(0.99)], tau=0.95, min_cluster=0) is False
