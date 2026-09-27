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
  Parent `manage_subagents action=read` returns pageable retained output;
  `include_recovery_context=true` adds a bounded, owner-scoped context excerpt.
  Full child ledger/timeline remains the durable audit record.
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
| Child mini-goal lifecycle, including model-selected deadlines | Live child 6 ran 3m41 despite timeout_seconds=5 and completed 11 tools/7 checkpoints; semantic completion and broader final-image lifecycle acceptance remain open |
| Real provider interruption after completed effects | Unit/integration covered; live safe fixture pending |
| Parent and child tools/permissions | Real QA children 1/3 completed files and checks with inherited host policy; child 2 reasoning loop identified and stopped, guard verified against captured stream |
| Restart recovery and durable child configuration | Open; restart currently marks child interrupted and delivers retained work |
| Independent context and automatic compaction | Live child 5 retained 23 checkpoints and compacted once. Popup explains 75% of saved 65,536 usable-input cap = 37.3% of 131,840 window. Ordinary post-Goal checkpoint reuse remains open |
| Parent works while children run, receives results asynchronously | TelemetryLab children 4/5 started 109ms apart while parent worked; child 6 real tools, final result readback and durable delivery verified. Restart recovery remains open |
| Goal ordinary-question timeout vs permission gates | Existing prior slice; recheck new realistic fixture |
| Plan follows current revision over long work/compaction | TelemetryLab reached 6/6; premature complete_goal was rejected until Plan updated; Goal completed attempt5. Independent host suite 85 tests passed. Not global acceptance |
| Safari reload and independently authenticated second browser | Passed active QA reload/old thinking/scroll-return on release-5ac5593; repeat after next release |
| Newest history, accurate counts and live/replay parity | Restored 53 lost round views from full journal; active counter reached 64 in Opera, next release and broader sustained acceptance remain open |
| Comparison with Codex, Claude and ZCode | Research started; map concrete gaps rather than adopting features speculatively |
| Git/Jetson exact image, regression and rollback verification | release-67c9d86 exact Git/runtime/helper hashes, full 7,532 passed, isolated 102 passed, host-only cwd tools and cross-browser delivery verified; next slice must repeat release checks |
| Subagent list polling stays bounded as output grows | Real six-child baseline 695,282 bytes. New fixed-size metrics projection passes 84 related tests; detail/result preserved. Final production byte comparison pending |
| Ordinary follow-up preserves working context after Goal completion | Open: source coverage seal, exact owner/run anchor, edit/delete/concurrent-guidance invalidation, fresh authorization and chronological suffix required; no blind restore |
| Background command continuation preserves selected workspace/policy | Command cwd/result delivery verified on host-only path. Direct bg_monitor follow-up lacks full foreground context; shared authorized continuation still required |
| Continuous final-image six-hour interactive acceptance | Open; old probe logs are not proof for newer images; no continuous probe on release-67c9d86 established |

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

## Primary-source comparison (2026-09-27)

- [Codex mailbox wait](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs):
  a wait timeout is not child cancellation. Odysseus now separates joining from
  lifetime, removing the aggregate model-selected child deadline. This source
  does not imply that every provider request must be unbounded.
- [Codex compaction](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs):
  persisted replacement history should match live history. Our live QA exposed
  summary-only downgrade and route-view divergence; corrected with immediate
  durable post-compaction ledger and both-route restoration.
- [Claude subagent resumption](https://code.claude.com/docs/en/sub-agents#resume-subagents)
  and [API errors](https://code.claude.com/docs/en/sub-agents#api-errors-in-subagents):
  documentation describes resumable transcripts and reporting partial work on
  failure. Candidate restores a waiting child's exact tool ledger; finalizer
  identity fencing prevents an older wait turn evicting its resumed successor.
  Process-restart recovery is still OPEN: durable execution config, refreshed
  authorization and unsettled-effect reconciliation are prerequisites.
- [ZCode subagents](https://zcode.z.ai/en/docs/subagents): the documented background
  mode permits parent work and automatically returns child results; model/tool
  selection and isolated contexts are explicit. This is documentation evidence,
  not a verified implementation or performance claim. Odysseus QA has directly
  exercised independent child contexts/tools and autonomous delegation; repeat
  durable delivery/reload checks on the final image before full acceptance.
