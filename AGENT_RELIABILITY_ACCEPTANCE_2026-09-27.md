# Long-running Agent acceptance — 2026-09-27

This is an open acceptance ledger, not a completion declaration. Main-chat
message bodies, objectives and host commands are excluded from diagnostics.
Jetson network, VPN and Wi-Fi configuration are outside scope.

## Confirmed production mechanisms

- Child ordinal 3 failed on provider HTTP 400 after 86 tool starts/results.
- Child ordinal 5 reached its model-requested 1,800-second whole-run deadline.
- A newer child failed on HTTP 500 after 21 tools; another failed compaction.
- The old child harness retried only once, only before the first tool, using a
  small text-based transient-error list. It discarded partial output on retry.
- Failed-child delivery selected only `error`, omitting retained child work.
- Some agent terminal failures have a `failure` payload without `failed=true`;
  the old child harness could publish these as successful if earlier prose existed.

## Current implementation slice

- Structured provider HTTP errors (400–599) and recognized transport failures:
  ten retries with bounded exponential backoff, cancellation remains effective.
- Resume from the latest committed child context ledger, retaining native
  tool-call IDs/results and compaction count. Never replay across an unsettled
  tool-start boundary. Each execution attempt has a unique effect-call namespace.
- Successful post-tool checkpoints reset the consecutive failure streak.
- Round-slice exhaustion continues from that ledger rather than declaring the
  child's earlier progress prose a final answer.
- Partial output survives failure and is delivered as explicitly incomplete.
  Parent `manage_subagents action=read` includes a bounded, owner-scoped context
  excerpt. Full child ledger/timeline remains the durable audit record.
- Terminal `failure` is honored even without the legacy `failed` boolean.

Focused regression: 93 passed (recovery, child runtime, delivery, agent rounds).
The HTTP-after-tool recovery test failed on the prior implementation, then passed.
Isolated Jetson candidate: 93 passed, alternate-port health HTTP 200.
Full suite: 7,467 passed, 25 skipped, 113 subtests; one repository-layout failure
because this ledger was initially placed in `docs/` (reserved for non-Markdown
site assets). Moved to the repository root; no test weakened. Final-image
browser acceptance pending.

## Remaining acceptance requirements

| Requirement | State / required evidence |
|---|---|
| Child mini-goal lifecycle, including model-selected deadlines | Partial; whole-run deadline still exists; distinguish explicit user budgets from model defaults before changing semantics |
| Real provider interruption after completed effects | Unit/integration covered; live safe fixture pending |
| Parent and child tools/permissions | Existing inheritance retained; real project verification pending |
| Restart recovery and durable child configuration | Open; restart currently marks child interrupted and delivers retained work |
| Independent context and automatic compaction | Open live test; investigate observed 35% vs configured 75% without conflating threshold basis or economic compaction |
| Parent works while children run, receives results asynchronously | Existing mechanism; realistic multi-module workload pending |
| Goal ordinary-question timeout vs permission gates | Existing prior slice; recheck new realistic fixture |
| Plan follows current revision over long work/compaction | Existing prior slice; long-run acceptance pending |
| Safari reload and independently authenticated second browser | Pending on final image under active work |
| Newest history, accurate counts and live/replay parity | Pending on realistic large fixture; synthetic history alone is insufficient |
| Comparison with Codex, Claude and ZCode | Research started; map concrete gaps rather than adopting features speculatively |
| Git/Jetson exact image, regression and rollback verification | Pending current slice |

## Safe project acceptance fixture

Build an offline, standard-library-only Python telemetry analysis CLI in a new
dedicated QA workspace. CSV validation/import to SQLite, parameterized queries,
deterministic statistics, JSON/CSV export, streaming large-file handling,
transaction rollback/idempotent imports, CLI error codes, tests, benchmark and
documentation. Give independent children non-overlapping modules and require
parent integration, additional adversarial tests and evidence review. No network
services, packages, credentials, privileged commands or changes outside that
workspace. Use the configured Qwen3.8 27b route, avoiding busy model :2.

Observe real Safari creation/input, tool execution, parallel children, parent
work, Plan updates, checkpoint/compaction, reload and second-client attachment.
Do not confuse elapsed idle monitoring with sustained active-run acceptance.
