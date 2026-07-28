// GENERATED FROM THE SERVER SOURCE — DO NOT EDIT BY HAND.
//
// Regenerate with:  python scripts/gen_harness_constants.py
// A hand-edit, a renamed verb, a widened drift tier or a new status literal is
// caught by tests/harness/test_constants_drift.py in the server's own suite.

/** Every advertised tool name. */
export const VERBS = [
  "hive_capture",
  "hive_flag",
  "hive_health",
  "hive_outcome",
  "hive_prune",
  "hive_recall",
  "hive_supersede",
  "hive_write",
] as const

/** The verbs this harness reasons about by role. */
export const VERB_CAPTURE = "hive_capture"
export const VERB_FLAG = "hive_flag"
export const VERB_HEALTH = "hive_health"
export const VERB_OUTCOME = "hive_outcome"
export const VERB_PRUNE = "hive_prune"
export const VERB_RECALL = "hive_recall"
export const VERB_SUPERSEDE = "hive_supersede"
export const VERB_WRITE = "hive_write"

/** The drift verdicts that mean the anchor MOVED — the tier the server acts on. */
export const QUALIFYING_DRIFT = [
  "anchor_changed",
  "anchor_missing",
  "blast_radius_changed",
] as const

/** A maintenance call the server honored. An allow-list: unknown fails safe. */
export const AFFIRMATIVE_STATUS = [
  "flagged",
  "pruned",
  "superseded",
] as const

/** Nothing the caller asked for happened. */
export const NON_AFFIRMATIVE_STATUS = [
  "disabled",
  "noop",
  "refused",
  "rejected",
] as const

/** A call that did its own non-maintenance job (a landing, an acknowledgement). */
export const OTHER_STATUS = [
  "approved",
  "quarantined",
  "recorded",
  "redacted",
] as const

/** The one status that means nothing was stored. */
export const STATUS_REFUSED = "refused"
