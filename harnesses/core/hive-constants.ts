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

/** The one status that means nothing was stored. */
export const STATUS_REFUSED = "refused"

/** The argument keys a memory's code binding or repo scope rides on. */
export const CARRIER_ARGS = [
  "anchors",
  "repos",
] as const

/** The argument key a recall's question rides on. */
export const QUERY_ARG = "query"

/** The argument key a memory's own text rides on. */
export const TEXT_ARG = "text"

/** The argument key a memory's code bindings ride on. */
export const ANCHORS_ARG = "anchors"

/** The id-bearing arguments of each verb that can name a memory to retire. */
export const ID_ARGS: Readonly<Record<string, readonly string[]>> = {
  "hive_flag": ["a", "b", "winner"],
  "hive_prune": ["episode_id"],
  "hive_supersede": ["loser", "winner"],
}

/** The argument key a store rides its own retirement rider on. */
export const REPLACES_ARG = "replaces"
