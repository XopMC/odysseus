# Engineering workspace implementation ledger

## 2026-09-15 Jetson engineering toolchains

The persistent Jetson runner now has executable, user-scoped toolchains for
Python/Pyright, TypeScript, C/C++/CUDA, Swift, Go, Rust and Solidity. The runner PATH
contains CUDA 12.6, the user Node runtime, Go, Rust and user LSP binaries; this
is deliberate so a systemd-launched runner sees the same tools that discovery
reports. Real runner start/stop probes passed for Python, TypeScript, C/C++, Swift,
Go, Rust and Solidity. C++, CUDA (`nvcc`), Swift, Go, Rust and Solidity (`solc`) smoke
programs also compiled successfully.

Xcode and Metal are not reported as available on Jetson: they are macOS-only
toolchains and require the configured Mac execution host. This deployment does
not falsely mark DAP, profile UI, preview/trace or cross-host Git transfer as
finished; those remain separate subsystem releases. Swift 6.3.3 for Ubuntu 22.04
aarch64 was installed only after a successful PGP verification of the official
release signature, then Swift compilation and SourceKit-LSP start/stop passed.

## 2026-09-14 consolidated Team engineering regression

Production image `odysseus:jetson-engineering-20260914-requirements-ui12` is
healthy. The exact ARM candidate completed the isolated no-network regression
with **6550 passed, 33 skipped, 128 warnings** in 394.81 seconds. This release
includes durable Team recovery, endpoint-qualified model selection, project
policies and runner-backed terminal/files/Git operations, bounded verified
browser evidence, Russian Team/Engineering controls, configurable context
policy, owner-scoped project memory, and owner-approved requirements supplied
only to local Team models. A real Jetson-to-Mac runner smoke command completed
with exit code 0 and its output was recovered through `terminal.poll`.

The absence of a fixed participant count is intentional: Team workers are
paged, and dispatch is constrained by explicit per-task concurrency settings,
approved budget and shared backend resource groups rather than a number of
models. The Jetson group remains serial by configuration. This is not a claim
that every roadmap stage is complete: cross-host source transfer/worktrees,
preview/trace UI, DAP/profiling and paid-provider execution still require their
own implemented and verified release stages.

## 2026-09-14 versioned project memory

Production image `odysseus:jetson-engineering-20260914-memory-ui9` is healthy.
The Engineering workspace now exposes owner- and project-scoped memory records
with a source, review state and compare-and-swap revision. Both save and delete
need explicit confirmation, produce replayable project events, and reject stale
concurrent edits rather than overwriting another device. Only verified memory
is injected into local Team-model prompts, as labelled evidence rather than
instructions; remote endpoints receive no memory through this path. The
candidate ARM regression passed **26 tests, 1 skipped** before this bounded
prompt scope was added; the follow-up candidate verifies the scope separately.

## 2026-09-14 reviewed Browser MCP evidence

Production image `odysseus:jetson-engineering-20260914-browser-ui8` is healthy.
Reviewed Team MCP now keeps a bounded Browser screenshot only through a
server-owned owner/task artifact sink: PNG/JPEG content is base64-validated,
size- and count-limited, stored outside SQLite under opaque identifiers and
served only through the owner-scoped Team artifact route with `no-store` and
`nosniff` headers. The model receives only an artifact reference and a warning
that browser output is untrusted evidence; a screenshot is never treated as a
passing verification result. Without this sink, the generic Team MCP dispatcher
continues to reject image results fail-closed.

The browser MCP itself reconnected after deployment with 30 tools. The fresh
no-network ARM regression for this exact source/image passed **6546 tests, 33
skipped, 128 warnings** in 394.04 seconds. This adds browser evidence handling,
not a preview server, trace viewer, external-account approval flow or completion
of Stage 6 / the overall engineering roadmap.

## 2026-09-14 isolated policy and Jetson Python LSP

Production image `odysseus:jetson-engineering-20260914-isolation-ui3` is
healthy with restart policy `always`. An explicit isolated project policy now
requires the enabled feature flag and an authenticated runner capability check;
approved checks dispatch only to `sandbox.command.start` in the retained
verification copy. The policy never falls back to trusted-host execution.
Targeted ARM regression passed 74 tests (2 skipped).

The initial overlay release exposed a candidate-sync path error: several
`src/` files had been copied to the candidate root and were therefore not
included by `Dockerfile.team`. It was corrected before `isolation-ui3`; the
running container was inspected to confirm the feature gate in
`engineering_store.py` and isolated dispatch in `engineering_check_runner.py`.

The complete no-network ARM regression, run through
`scripts/test_engineering_candidate.sh` from a full source snapshot copied to
a disposable container filesystem, passed **6540 tests, 33 skipped, 105
subtests** in 392.62 seconds. Production databases, credentials and network
were not mounted into that test container.

Jetson Python LSP is live: Pyright is installed under the `xopmc` user, and the
broker uses a private Node 22 runtime only for language-server processes, not
for system services or task commands. A real runner session opened a `/tmp`
Python document, returned `textDocument/documentSymbol`, and was stopped.
Other language servers remain accurately unavailable until installed and
tested. This does not complete stages 4, 9 or the full IDE roadmap.

## 2026-09-14 isolated-runner primitive

The Jetson user host runner now has `sandbox.command.start`. It accepts only a
command and a retained verification-copy workspace; the image, mounts and
Docker security flags are server-owned. A real Jetson smoke test rejected a
normal user workspace, then completed in the verification copy using the
pinned Alpine digest with network disabled and no Docker socket. The targeted
ARM regression passed 47 tests. This is deliberately not marked as completion
of stage 3: the public project-policy/UI flow and scoped-secrets work remain
unreleased, and a container is not an absolute hostile-code boundary.

## 2026-09-14 LSP bridge delivery

Production image `odysseus:jetson-engineering-20260914-lsp-ui` is healthy with
restart policy `always`. The persistent Jetson runner now supports the bounded,
read-only LSP protocol and the Russian Engineering UI exposes a discovery panel.
The ARM candidate passed 29 targeted route, runner, UI and localization tests.

A real cross-host probe started and stopped the Mac C/C++ language server in
`/tmp`: definition, references, hover and document-symbol capabilities were
reported by the actual server. Jetson currently has no language servers
installed, so the UI accurately returns `not_installed` rather than claiming
support. This does not complete code indexing, full IDE support, DAP or the
remaining roadmap stages.

## 2026-09-14 engineering-foundation release

Production image is `odysseus:jetson-engineering-20260914-release`. The Team,
Engineering, Context Policy and reviewed read-only Team MCP flags are enabled.
The release retains the existing host runner and all explicit project, host,
external-data and command confirmations. It does not enable unavailable
isolation, cross-host worktrees, LSP, debugger/profiler or experiments.

The running container is healthy with restart policy `always`; the host runner
is active. Post-deployment isolated ARM tests passed: 85 tests and 10 subtests
in 12.34s for Team runtime/collaboration, replay attribution, Russian menu
coverage, engineering routes and context policy. Browser verification after the
deployment confirmed Team Basic setup, non-truncated endpoint-labelled model
choices, Engineering and Context Policy panels, and the corrected Russian
notification / legacy-project status text. Backups made before the two release
changes remain under `/home/xopmc/services/` and roll back compose/image only;
they must never overwrite later chats or task data.

The user-approved 16-section plan is the contract. This file records evidence,
not a claim that the entire roadmap is released. Production now runs the legacy
compatible bridge `odysseus:jetson-engineering-20260914-bridge2`, with the verified
Russian UI and realtime fixes. New engineering modules remain candidate-only
until their integrated release gates pass.

## Invariants

### Latest user priorities: replay fidelity and simple Team setup

- User reports remaining untranslated UI and collapsed multi-round rendering
  after refresh/second-device attach. Do not treat earlier Russian/realtime
  checks as proof these broad issues are resolved.
- Replay regression reproduced: agent_step appended content to one holder.
  Candidate now creates separate continuation bubbles and cleans all temporary
  holders on canonical reconciliation/navigation; tool cards remain per round.
  Actual chat.js VM tests: 11 cross-device scenarios plus fallback attribution
  tests pass. Full browser/production replay parity (thinking, tools, sources,
  timestamps, persisted-history boundaries) still needs acceptance.
- Added real-Chrome DOM regression `test_replay_browser_rounds.py`: desktop
  1280x900 and mobile 390x844 pages both retain two distinct assistant bubbles,
  completed tool output belongs only to the first round, the second stays
  separate, and terminal reconciliation removes temporary bubbles without
  clearing the draft. Fresh test passed. It executes the actual resumeStream
  source with controlled transport/dependencies, not production network/history
  or full-app startup. Screenshot/visual parity is not established by this test.
- Team defaults to Basic setup: leader and selected worker pool, automatic
  dispatch/continuation. Advanced keeps fine project/manual worker controls.
  Permissions/budgets remain explicit; hidden manual assignments are excluded
  from Basic requests, without erasing Advanced drafts. New dedicated tests
  pass (real Chrome fixture with six routes; payload contract with 70 routes,
  denied unauthorized external use); existing full Team browser fixture passes
  after explicitly selecting Advanced and opening Access permissions.
- New labels translated in i18n.js; whole-app Russian completeness not proven.
  Production has not been switched for these changes.
- Basic runtime regression now checks creation through TeamRuntime, not just
  the frontend payload: six selected endpoint/model pairs initially produce
  one planner; a completed plan becomes six durable assignments to the selected
  endpoints. Reconciliation does not duplicate workers and host access is never
  invoked when disabled. Real SQLite/coordinator, supplied planner result (not
  actual LLM inference). ARM snapshot `odysseus-qa-basicruntime-gFHn82` passed
  all 11 Team runtime tests. Real model planning remains a separate gate.
- Real local planning smoke now passed using Ornith
  `ornith-1.5-35b-a3b:q4_k_m` at Jetson :11434, actual TeamRuntime/team_model
  transport, isolated temporary SQLite, fixed local resolver and forbidden host
  callback. Produced exactly two independent read-only tasks with objective,
  acceptance, participant=0 and empty write_scope; worker status done; process
  exit 0. Harness: `../odysseus-debug/basic_planner_live.py`, candidate source
  `odysseus-qa-basicruntime-gFHn82`. Result validates real plan generation but
  does not distinguish native tool calls from supported JSON fallback, and
  does not prove multi-model execution, full UI flow or end-to-end acceptance.
- Saved-plan coordinator now rejects noninteger/missing participant indices
  instead of coercing False, 0.5 or "0" to participant 0. Regression reproduced
  four invalid cases before the fix; after it all plan nodes are validated before
  assignment and no partial workers appear. ARM runtime/collaboration tests:
  31 passed + 7 subtests, exit 0. The temporary basicruntime snapshot was updated
  only after its previous test process exited. This fix postdates UI3 image.
- Follow-up localization found ten static Team explanations/headings bypassing
  translation (project profiles, external scopes, checkpoints and integration).
  These now use explicit uiElement bindings and Russian catalogue entries.
  A regression rejects remaining literal paragraph/legend/h4 text passed through
  the untranslated element constructor; dynamic user values remain untouched.
  Six localization tests and the full existing Team browser fixture passed
  (`odysseus-team-ru-labels-09i8dizf`). This does not cover all dynamic labels,
  aria labels, unrelated menus, or prove production deployment.
- Team accessible names now use language bindings too: host scope, preset,
  saved profile, external data scope, result, terminal and checkpoint status.
  Missing Terminal translation was caught by the new authored-name regression
  and added. Six localization tests plus existing Team Chrome workflow pass
  (`odysseus-team-aria-0ttghmwt`). These changes postdate the frozen integrated
  ARM snapshot `odysseus-qa-integrated-Kmzuss`; that full run does not cover them.
- Conditional Team terminal/diff explanations and unnamed artifact fallback now
  use translation bindings; supplied artifact names remain verbatim. Six
  localization tests plus the existing Chrome workflow passed
  (`odysseus-team-conditional-361k7id1`). These are later candidate-only changes.
- General Settings follow-up: fallback removal tooltip, OS-default voice
  placeholder, TTS saved/failed/provider-required and preview failure messages
  now use localization. Ephemeral statuses use t() so clearing them cannot leave
  a stale persistent binding; authored tooltip/placeholder use bindUiText.
  Settings native ESM coordinator and leaf harness pass, JS parses, six menu
  tests pass. Actual TTS playback and rendered locale-switch flow are unverified.
- Shared transient settings statuses: 23 further lines now call t() for Saved /
  Failed to save. New executed-statement regression initially failed because
  both catalogue keys were absent, revealing the earlier TTS assertion gap.
  Added the missing Russian entries; seven localization tests now pass,
  including 25+ actual status assignments in RU/EN and cleared notices staying
  empty. Settings ESM coordinator passed after the mechanical replacement.

- Preserve chats, models, settings, port 5130, host access and runner commands.
- No fixed team/pool ceiling. Preserve physical backend and budget scheduling.
- New projects start read-only until explicit execution-mode selection.
- No paid API probes, host installation, or capability elevation by discovery.
- Feature gates default off; unavailable adapters must say unavailable.
- Never restore old databases over newer user data during rollback.

## Ordered acceptance ledger

| Stage | Scope | Status |
|---|---|---|
| 0 | Additive durable project/policy foundation and rollback compatibility | In progress |
| 1 | Shared tool catalogue, dispatch policy, model capability probes | In progress |
| 2 | Portable runner, host registry, cross-host workspaces | In progress |
| 3 | Verified isolated execution and scoped secrets | Pending |
| 4 | Code index and real language adapters | In progress |
| 5 | Baseline/check runs and requirements evidence | In progress |
| 6 | Scoped browser, preview, traces | Pending |
| 7 | Versioned project memory and context integration | Pending |
| 8 | Isolated solution experiments and measured model profiles | Pending |
| 9 | Real DAP/profiling sessions | Pending |
| 10 | Paged large teams and fair adaptive resource scheduling | Pending |
| 11 | Two-host, two-client, six test endpoints, final two-hour release gate | Pending |

Implementation records must distinguish unit/contract fixtures from real model,
browser, host and toolchain evidence. No incomplete stage is production-ready.

### Full regression environment

### Context2 compatibility rehearsal

Latest UI3 rehearsal: `odysseus:jetson-engineering-20260914-rehearsal-ui3`,
image `sha256:401b097163dce93b9ae8433a32b5b97b5b41da55ee5b07f421e0dba3898f8b56`,
source `odysseus-qa-releasecheck-WUYHdR`. Old→candidate→old passed on private
copies: chat rows, new writes and full/partial presets preserved. Evidence:
`/home/xopmc/services/odysseus-migration-rehearsal-e7sukxos/result.json`.
Production verified afterward: bridge2 healthy, restart always. No new feature
flags enabled during this startup/compatibility check; no production deployment.

Built `odysseus:jetson-engineering-20260914-rehearsal-context2` from snapshot
`odysseus-qa-presetmigration-VtvyQt` and pinned dependency base `9f3eae016fb3…`.
Candidate image ID: `sha256:cc0884d7fa7d78ea82c2bf82c1583db8ddf71bdf3a5e5477147f3513eccadc3d`.
The private no-network old→candidate→old rehearsal passed on copied SQLite data.
It now writes and verifies full and partial context presets as well as the prior
task/policy canaries. Chat rows, new writes and preset kinds/values survived rollback.
Evidence: `/home/xopmc/services/odysseus-migration-rehearsal-knrncr7s/result.json`.
Production was freshly checked afterward: bridge2, healthy, restart always.
This proves compatibility/startup on private per-database backups, not live task
recovery, the final duration gate, complete features or production deployment.

### Regression runs

Integrated ARM snapshot `/home/xopmc/services/odysseus-qa-integrated-Kmzuss`:
6534 passed, 101 subtests passed, 33 skipped, 128 warnings in 391.44 seconds.
Includes separate replay bubbles, Basic/Advanced Team configuration, context
retention fixes and the first Team explanation translations. The later Team
accessible-name bindings were verified separately and are not in this snapshot.
Browser tests requiring Playwright are skipped in this dependency image; local
Chrome fixture results above remain separate evidence, not live model acceptance.
No deployment or completion of remaining roadmap stages follows from this result.

Latest full no-network ARM regression on the frozen source
`/home/xopmc/services/odysseus-qa-search-00Xd4U` completed successfully:
**6518 passed, 101 subtests passed, 31 skipped, 128 warnings**, exit 0,
391.89 seconds. This includes context preset storage, API, rename and search.
The subsequent authored Engineering-label inventory and one additional Russian
API error translation passed the five local dynamic-menu tests separately;
they are not part of that frozen full-suite snapshot. Browser profile workflows
were tested separately in Chrome against HTTP fixtures (see CONTEXT_POLICY_PLAN).
This is not completion of the missing roadmap stages or hardware-duration gates.
Production remained the healthy bridge2 image during this run.

The first broad ARM run was intentionally interrupted after detecting a failure:
648 passed, 7 skipped, 1 failed, 2 subtests. The failure was missing
`/app/.gitignore` in the old selectively mounted image, not an application defect.
The replacement harness `scripts/test_engineering_candidate.sh` consumes a full
frozen source snapshot, refuses a root .env/data directory, mounts it read-only,
copies it into disposable container storage, disables networking and caps CPU/RAM.
No production database, socket or host credentials are mounted.
Snapshot: `/home/xopmc/services/odysseus-qa-source-uJNcCq`.
The formerly failing application structure suite passed all 12 tests there.
Broad fail-fast run reached 84%: 5431 passed, 30 skipped, 25 subtests, one failure
in the Settings ESM test loader: its explicit real-module allowlist omitted the
new i18n dependency. Adding the real module (not a stub) preserves coordinator
assertions; the local Node coordinator passed. Remaining s–z regression is running
against a new snapshot `/home/xopmc/services/odysseus-qa-source-zw1KAf`.
Neither the interrupted run nor these incremental shards are final release acceptance.
The first s–z shard exposed collection-order pollution: a legacy parser test replaced
the unloaded src.agent_tools package with MagicMock, preventing the real web_tools
import. conftest now preloads the real package, matching the existing core.models
protection, without removing any assertions. A targeted ARM run of sanitization,
search reliability, session tools, skill/tool prompt-injection and Settings ESM
passed 30 tests. The s–z shard was restarted after this concrete collection fix.
That run passed 661 tests before a startup-session fixture error: the fixture
copies sessions.js into a temporary directory but had not redirected its new
i18n import. It now imports the real localization module by its repository URI;
all three startup-session bootstrap tests passed on ARM (exit 0). No assertions
were removed and production code was not changed to accommodate the fixture.
The rerun stopped with 1167 passed, 1 skipped, 71 subtests passed and three
failing subcases in AgentRegistryDispatchTests: manage_notes was advertised but
the mocked implementation was not awaited. In fresh isolated processes the entire
tool registry suite passed (26 tests, 45 subtests), and all test_tool_*.py passed
(177 tests, 45 subtests). The order-dependent failure remains unresolved; neither
isolated success nor overlapping counts establish release acceptance. The failing
assertion now includes returned stream events to locate the first bad boundary
on the next broad reproduction, without weakening its exactly-once requirement.
That reproduction showed a real notes query against the empty test database,
not a denied/missing tool. Reduction isolated collection-time module eviction in
test_web_search_raw_json_tool_call.py and test_web_search_time_filter.py: these
re-imported tool_execution while agent_loop retained the previous wrapper. A
four-file regression (tool_policy, tool_registry, both web parser files) failed
on canonical wrapper identity before the fix and passed 52 tests + 45 subtests
after removing the two destructive module resets and their redundant global stubs.
The parser assertions and permission/dispatch checks remain unchanged. A new broad
s–z run then passed 1510 tests and 76 subtests, with one skip (exit 0,
111.07 seconds). Production behavior was not changed for this fixture defect.
This is the remainder shard, not a new full-suite acceptance result.
The next full run on `odysseus-qa-source-9B7q4n` reached 6167 passed, 31 skipped,
96 subtests passed before canonical wrapper identity failed. Two more collection
resets in test_fenced_inline_args.py and test_fenced_invoke_no_raw_xml.py had the
same mechanism. A four-file reproduction failed before their removal and passed
72 tests + 45 subtests afterward. Inline/fence-boundary and no-raw-XML execution
assertions were retained. This full run is failed, not release acceptance.
After these fixes, the complete no-network ARM pytest run on the same snapshot
passed: **6512 tests, 101 subtests, 31 skipped**, exit 0 in 389.42 seconds.
This establishes regression evidence for that source cut, not the missing roadmap
features, skipped browser flows or final hardware/duration acceptance.
The newer complete snapshot `/home/xopmc/services/odysseus-qa-source-deqniM`
includes policy-history polling and its SQLite index. Its affected context policy,
store, Agent/Team runtime, Engineering API and foundation UI suite passed on ARM:
**50 tests, 8 subtests, 1 skipped**, exit 0. Browser workflows for these changes were
run separately in local Chrome. Neither result is a production deployment.

### Context history localization

Boolean overrides in policy history now render as translated Enabled/Disabled
labels rather than raw true/false. A Chrome HTTP-fixture workflow verified both
states in English and Russian, alongside existing scoped saves, conflict handling,
draft preservation and desktop/mobile flows (exit 0). Numeric values and scope
identities remain raw data. Browser plugin was unavailable; existing Playwright
workflow used installed Chrome. Evidence directory:
`/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-context-history-ru-z47_ngm2`.
This does not establish a physical two-device or full-menu localization pass.

### Background context policy revision checks

The existing context-policy GET now refreshes saved revision observations for every
selected scope, not just Team workers. A mount-owned five-second timer checks while
the document is visible; visibility restoration checks immediately. Requests reuse
existing coalescing and generation/scope fencing. Changes mark validation stale and
block save until explicit reload without replacing drafts or issuing POST/inference.
Disable/destroy clears the timer and destroy removes its visibility listener.
Chrome HTTP-fixture acceptance passed for a remotely changed owner revision without
a Team event, RU conflict text, preserved draft, no POST, simulated visibility pause/
return, and no requests after destroy. Evidence:
`/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-context-live-final-0wjayjal`.
Listener registration occurs only when the context feature is enabled and is
removed on disable as well as destroy. The foundation UI Node workflow with the
context feature absent also passed; Team's Chrome workflow passed with background
checks alongside its SSE observation refreshes. Final context Chrome repeat passed
after the listener lifecycle adjustment and stale-validation localization.
This is polling, not server push; physical two-device acceptance remains open.
The subsequent cycle also replays one policy-history page from its saved cursor.
Empty pages preserve rendered rows, and an error is replaced by the restored
history on a successful retry even if no new events arrived. Chrome verified
remote history arrival without a Team event, a transient 503, recovery without
duplicates, paused policy/history requests while hidden, and teardown cleanup:
`/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-context-history-recovery-ox8nnavv`.
An additive `(owner,seq)` index supports these polls. Eight local SQLite tests pass,
including 65 owner events interleaved with another owner's events, cursor replay,
query-plan index use and opening the old table without the index while preserving
the rows. This is policy history, not completed compaction history. The active full
ARM run at `odysseus-qa-source-9B7q4n` predates the history-poll/index changes and
cannot be cited as their integrated verification.

### Team state localization follow-up

Worker cards now bind known role/status labels separately from raw names, model IDs,
criteria and checkpoint IDs. Thirteen states, roles, scope heading and empty state
have explicit RU translations; unknown enum values remain raw, not guessed.
The browser test verifies RU/EN switching without translating user-authored data.
Native select height was visibly clipping labels; its minimum height now accounts
for font and padding. The test measures rendered label height (native Chromium
reports select line-height as normal), and the actual Chrome workflow passes.
The model selector separately binds Local model / External model and its accessible
Endpoint and model label for RU/EN. The name/endpoint suffix and exact JSON
endpoint_id/model option identity remain unchanged. The Chrome Team workflow
verified translated prefixes and preserved raw identities; this does not establish
complete localization of every other menu.

## Additional user acceptance requirements

- Configurable context policy and UI controls are part of the full objective.
  Detailed accepted addendum: `CONTEXT_POLICY_PLAN.md` (budget/reserve/threshold,
  retention, summarizer, checkpoints, multi-agent policies, RU/EN and acceptance).
  Status: partial runtime/UI integration behind an opt-in flag; remaining context
  requirements are tracked in the addendum. Do not mark the roadmap complete without it.
  Current context slice: Team snapshot now supplies exact task/worker selection to
  the existing policy panel. The server derives the task's project; the project
  picker for NEW tasks does not change inheritance of an existing task. UI keeps
  same-worker drafts across snapshot updates and clears old targets on chat change.
  Fresh Chrome context-policy and Team workspace tests passed (HTTP fixtures,
  real rendered modules, desktop/mobile RU); separate no-network ARM container
  check: `test_context_policy_store.py test_team_context_policy.py
  test_agent_context_policy.py test_engineering_routes.py` = 37 passed + 2 subtests.
  Local Python could not run runtime tests due to absent pytest/FastAPI; those
  tests ran in the application image instead. Production remained healthy bridge2,
  not switched to this candidate. Single-Agent task-scoped UI and full compaction
  lifecycle remain open.
  Portable context profiles: strict JSON v1, 32 KiB maximum, preview/merge into
  draft only, explicit existing CAS save. Export whitelists saved scope overrides,
  not chat/model/endpoint identifiers or unsaved edits. Node schema-negative test
  and both Chrome policy/Team scenarios passed, including actual downloaded JSON
  inspection and RU validation. Named server-side preset library is still pending.
  Team context observation: successful model returns persist their shaping policy,
  revision vector and output cap in the existing worker_metrics journal. The
  owner/worker-scoped context endpoint and RU panel distinguish that observation
  from newly saved policy; estimates and summary requests are explicitly labelled.
  Failed/unknown responses do not claim completion. Fresh affected ARM suite:
  49 passed + 2 subtests; real Chrome fixture verifies matching/stale revisions.
  This is request-boundary evidence, not live in-flight usage or an LLM quality test.
  Team SSE worker_metrics/open and Engineering-tab entry now refresh the selected
  worker observation without overwriting draft controls. Concurrent refreshes are
  coalesced; scope/generation fences ignore stale responses. Revision changes block
  saving until explicit reload; refresh failures retain data with a RU/EN stale
  warning. Browser tests exercise real modules with fixture SSE/HTTP, unchanged
  drafts, reconnect, remote revision conflict and failed refresh recovery.
  This does not yet subscribe to standalone owner-policy changes or prove physical
  multi-device acceptance on the final production build.

- English/Russian interface language selectable in Settings, persisted per owner.
  Translate interface strings only, never conversations, code, model IDs or URLs.
- Model picker endpoint addresses must remain fully readable without ellipsis,
  including equal model names across endpoints, on desktop and mobile.

### Endpoint picker evidence (candidate only)

Full origin/path is now displayed for every endpoint (credentials/query/fragment
excluded). Address wraps below the model name. Node route-identity interaction
test and actual Chrome/Playwright desktop 1440x1000 plus mobile 390x844 passed:
four same-name routes, exact endpoint selection, restoration after reload,
preserved draft, long path wrapping and no horizontal overflow. Production has
not been changed by these tests.

### Cross-device activity blocker

Production authenticated read-only probe established prompt SSE delivery: the
active replay contained delta, tool_start/tool_output, agent_step and heartbeat
events. The resume UI ignored tools and round boundaries until completion.
Candidate renders those events immediately with bounded per-tool output and
deduplicates SSE IDs within a subscription. Eleven JS lifecycle scenarios pass;
two real Chrome contexts (desktop/mobile) pass controlled SSE replay, reconnect,
draft preservation and completion reconciliation. Full production-page overlay
acceptance is tracked separately. This does not implement durable server cursors:
the existing in-memory run buffer remains a separate long-run release gap.

Realtime hotfix backup: `/home/xopmc/services/odysseus-realtime-backup-20260914.f9pB4c`.
Only `/app/static/js/chat.js` changed in the running container, atomically; no DB,
runner or model settings changed. Compose now selects the tested derivative
image for the next recreation. Full engineering flags are still off.

### Real language-server evidence

Opt-in `test_engineering_clangd_real.py` passed on local macOS with installed
clangd: actual C++ diagnostic, repaired diagnostic and cross-file definition.
This is not acceptance for all languages or cross-host workspaces. LSP runner
RPCs and project routes are candidate-only; isolated execution remains unavailable.

## Deployed UI release, 2026-09-14

- Image `odysseus:jetson-team-20260914-ru` layers only reviewed UI assets on the
  verified realtime image. Current container received the same assets without
  a restart; started-at remains `2026-09-13T20:51:41.767889188Z`, health healthy.
- Backup assets/config: `/home/xopmc/services/odysseus-ui-ru-20260914.4lZz4V`.
  `original-static` and `original-compose.yml` provide UI rollback without DB
  restoration or changes to current chats. No rollback was needed/applied.
- Locale uses existing per-user preferences API. Real backend integration was
  checked through a loopback staging proxy and then directly on deployed 5130.
  EN/RU change, persistence/reload, 13 Settings navigation entries and untruncated
  endpoint labels passed with no page errors. Prior user preference restored.
- Inventory tests: 310 unique authored static menu/select labels and 255 dynamic
  menu entries (231 unique labels), no missing dictionary keys. This is menu
  coverage, not a claim that every explanatory paragraph/error is localized.
- Deployed realtime was rechecked directly, with two independent authenticated
  browser contexts at 1280x900 and 390x844. Both saw active tool/round events;
  zero page errors and no Stop requests. Closed browsers did not stop the run.
- Candidate engineering subset: 291 passed, 4 skipped, 83 subtests in isolated
  ARM container. It is NOT a complete application regression or release gate.
  Full engineering roadmap remains incomplete and disabled in production.

The HTML route-fulfillment QA attempt hit Chrome local-network address-space
checks; it was replaced with a real HTTP staging proxy without disabling browser
security. That harness failure was not worked around in application policy.

## Follow-up engineering candidate, 2026-09-14

- Detached subscribers now carry one coalesced wake-up rather than an unbounded
  duplicate event queue. Optional `ODYSSEUS_DURABLE_CHAT_REPLAY=1` stores replay
  artifacts outside SQLite, with on-disk indexes, a 256 MiB per-run / 1 GiB total
  ceiling and seven-day terminal retention. Limits stop the producer explicitly.
  GET `/api/chat/replay/{session_id}` is owner-gated and paged; an unfinished
  archived run is `interrupted`, never claimed to be executing after restart.
  This is NOT automatic single-agent execution recovery or complete UI archive
  integration. Flag stays off until the final runtime release gate.
- Seven local replay tests cover 2,000-event slow clients, restart/cursors,
  storage limits, unknown side-effect non-retry, and failed replacement storage
  allocation preserving the active predecessor. Earlier surrounding runtime
  slice passed 64 tests on ARM; final integrated counts are recorded separately.
- Compatibility bridge patch guards legacy production against tagged runtime
  tasks; candidate guards direct claim/model/tool/host seams as well as pump.
  25 candidate and 10 patched-legacy tests passed on ARM. The patch does not
  grant old code knowledge of the new runtime and does not restore old data.
- Synthetic local model probe has actual HTTP/SSE/tool-result integration tests
  (35 tests, 3 subtests including adjacent transport). Public interactive-owner
  routes require a current configuration digest and explicit confirmation;
  external paid probes remain unsupported, with no provider request sent.
- Check-evidence persistence is additive to TeamStore: approved versioned check
  profiles, baselines, immutable runner-authenticated results and requirements.
  Missing verifier, changed code/policy/profile and unverified requirements block
  completion. 15 foundation/store tests passed. No execution adapter or UI for
  this check foundation is claimed implemented.
- Broad app test attempt reached 449 passed / 6 skipped but was interrupted on
  a packaging-fixture failure: test expected `/app/.gitignore`, absent in the
  release base. This is not a completed full regression. Targeted rerun includes
  the real checkout `.gitignore`; no production code workaround was introduced.

The user chat was initially active, so the UI was hotpatched first. It later
finished; a fresh authenticated preflight found zero running chats, and the
Team DB contained only one cancelled task. Only then was the bridge deployed.
The full 16-section plan and final two-hour candidate gate are still incomplete.

### Fresh integrated evidence

- ARM integrated regression: **393 passed, 3 skipped, 86 subtests passed**,
  including engineering modules, Team, runner, owner-scoped replay routes,
  chat lifecycle/replay/context and the app-structure fixture. Three skips do
  not count as accepted functionality. This is a targeted suite, not all tests.
- Candidate image: `odysseus:jetson-engineering-20260914-rc1` (not production).
  Compatibility image: `odysseus:jetson-engineering-20260914-bridge`.
- Actual bridge → candidate → bridge boot/health and SQLite preservation
  rehearsal passed in `/home/xopmc/services/odysseus-migration-rehearsal-95vvt27v`.
  Additional expanded engineering/check tables and a tagged canary task were
  retained/read by the bridge; it explicitly refused that new-runtime task.
  All this used a consistent private backup, no production data restoration.
- Real-browser candidate proxy acceptance now also opens an existing chat's
  context popup: Russian title/count-source rows render correctly, alongside
  persisted language and readable endpoint labels. No page errors.

### Production intermediate release

Current private schema rehearsal (not deployment):
- Updated `../odysseus-debug/rehearse_team_release.py` requires explicit distinct
  rollback/candidate refs, resolves image IDs, rejects aliases for the same image,
  and keeps Team/host/Engineering/context/background task features disabled at boot.
  Five offline harness tests passed. Canary initializes current projects, checks,
  operations and context schemas without granting project execution.
- Built `odysseus:jetson-engineering-20260914-rehearsal-context1` from snapshot
  `odysseus-qa-source-deqniM` and tested dependency image
  `sha256:9f3eae016fb3e2a8f09c8a24cc4e1ec4b06058a3b78e116e9b112cd415dd0e42`.
  Base source is retained outside active `/app`, avoiding stale overlaid modules.
- Actual bridge2 → candidate → bridge2 boot/health rehearsal passed on private
  per-database SQLite backups at
  `/home/xopmc/services/odysseus-migration-rehearsal-crjua5zi`.
  Candidate ID: `sha256:f3ace83747d000da66a428c44549176f90b2f2c06197bca2f78378e075197c2c`.
  Existing chat/session row fingerprints were preserved; integrity checks passed;
  the project-bound canary, context override and policy event survived rollback.
  No network, published ports, host credentials or enabled task scheduler were used.
- All rehearsal containers exited and were removed; private copied data/result
  remain restricted for inspection. Production still reports bridge2, healthy,
  restart=always. This does not prove live-task recovery, a coordinated cross-DB
  snapshot under concurrent writes, every setting migration, or final full release.

- Eleven JS assets deployed atomically, then packaged in
  `odysseus:jetson-team-20260914-ru2`. UI backup:
  `/home/xopmc/services/odysseus-ui-ru2-20260914.KhHIbR`.
- Final locale slice: 18 focused tests passed, including actual Chrome message
  actions, image actions, OCR, metrics, context controls, Theme colors and
  EN/RU switching across cloned controls. Additional authored constructor
  inventory: 186 labels / 129 unique keys, no dictionary gaps. Technical values,
  user text, names, source code, HEX values and some long explanations are not
  translated. This is concrete tested coverage, not every possible UI path.
- After authenticated idle preflight, installed
  `odysseus:jetson-engineering-20260914-bridge2`, containing only that UI plus
  the previously verified compatibility guard. Backup/config/SQLite snapshots:
  `/home/xopmc/services/odysseus-bridge2-backup-n0_qfxeh`.
- Health is healthy, restart policy remains `always`; runner MainPID unchanged.
  All pre-release chat rows were compared and preserved. Model configuration,
  host-runner code and data were not replaced. Rollback never restores old DBs.
- Final active-replay browser retry had no running chat to observe (the user run
  had finished). It is recorded as unavailable, not passed. Earlier active-run
  two-client evidence and deterministic replay regressions remain as recorded.

### Candidate diagnostics and check-runner integration (not deployed)

- Model-probe operations now persist in the existing Team SQLite database.
  Interactive owner routes return an operation ID; paging, reload and cancellation
  do not resend inference. A fenced lease marks expired in-flight work interrupted.
  Queue recovery is allowed; unknown requests are never automatically replayed.
- Probe UI exposes exact endpoint/model identity, confirmation, saved operations,
  cancellation and RU/EN states. Controlled Chrome checks are not a real LLM test.
- Operation store/manager and authenticated routes: 13 focused ARM tests passed.
  Actual provider acceptance and final release regression remain outstanding.
- The check-runner adapter uses approved commands, durable dispatch identity and
  actual runner workspace hashes. Its separate subprocess/store regression slice
  passed 31 tests and 2 subtests. No production runner was replaced. Cross-machine
  compiler evidence and the remaining engineering stages are still open.
- The accepted configurable context-control scope remains documented in
  `CONTEXT_POLICY_PLAN.md`; it is not marked implemented by these diagnostics.

### Configurable context foundation and integrated catalogue check

- Added validated ContextPolicy with full-window reserves, schema accounting,
  trigger/target hysteresis and fail-closed insufficient-budget handling.
- Existing working-context compactor accepts explicit retention/summary/timeout
  settings and a target budget; preserves initial/latest goals and entire pinned
  tool exchanges. Failed/oversized summaries return the original working history.
  No-policy callers retain legacy defaults.
- Additive owner/project/task/worker policy storage, inheritance sources, exact
  ancestor revision vectors, correction of invalidated child overrides and paged
  owner events are exposed through interactive owner API. Saved profiles are not
  yet consumed by Agent/Team runtime, nor editable through the UI: this is partial.
- Fixed engineering finetune fenced/native parser mismatch, including fallback.
- Fresh combined ARM slice: 87 passed, 54 subtests; prior narrower context slice:
  60 passed, 6 subtests. First combined attempt used stale catalogue dependencies
  in the test snapshot and failed; syncing those files made the same command pass.
  No production deployment, model request or complete-release claim in this slice.

### Saved context profiles now affect Agent/Team and UI (candidate only)

- Explicit owner profiles shape Agent primary/fallback requests, cap actual
  output, preserve original history on failure, and block unknown-window or
  disabled-compaction overflow. Final synthesis also checks the hard budget.
- Team planner/worker/finalizer calls resolve inherited profiles each request;
  compaction replaces caller working context after validation, saves checkpoints,
  and rechecks task, endpoint, consent and policy revisions after awaits.
- Feature `ODYSSEUS_CONTEXT_POLICY_ENABLED=1` requires engineering enabled too.
  Missing saved profile preserves legacy behavior; no production flag changed.
- UI provides 11 typed settings, owner/project scope, explicit override/reset,
  source labels, event history, conflict recovery and RU/EN. Task/worker scoped
  controls, alternate summarizer, presets and manual durable coordinator remain
  open; this is not the full context addendum or complete engineering release.
- Combined ARM regression: 228 passed, 53 subtests. Actual Chrome controlled-HTTP
  context-policy workflow passed on desktop 1440x1000 and mobile 390x844.
  This verifies real rendered UI + separate runtime/store boundaries, not a
  production LLM or final two-hour acceptance run.
- Review found a separate large-list issue: probe history read only its first 50
  operations sorted by random UUID. The following slice addresses it.

### Paged diagnostic history and fresh production replay evidence

- Diagnostic history now uses newest-first time/ID keysets. A separate active
  filter returns oldest active work, so activity recovery is independent of the
  first history page. Cursor lookups remain owner-scoped and tolerate completion
  of the cursor operation; newer inserts do not duplicate previously paged rows.
- Backend slice: 36 tests and 3 subtests passed on the isolated ARM runner,
  including 71 records and an active operation older than the first 50 results.
  Root Chrome paging/active restoration check also passed (106 history entries,
  old active work outside page one); this is candidate UI, not deployment.
- Production remains `odysseus:jetson-engineering-20260914-bridge2`, healthy.
  Authenticated preflight found one streaming chat. No restart or real diagnostic
  model request was attempted during that user run.
- Fresh read-only replay QA against production (no candidate JS overlay): two
  browser contexts at 1280x900 and 390x844 joined the active chat. Initial partial
  replay differed, then both converged to identical rendered response text,
  49 response blocks and 54 tool cards. Drafts survived; no page errors or stop
  requests. This proves this active-run join/replay, not the entire release or
  the final two-hour fault/recovery acceptance.

### Timeout evidence and remaining Russian foundation labels

- Runner-backed checks now preserve authenticated `timed_out` independently of
  exit code. A deterministic runner-observation race test failed with `passed`
  before the fix and passes afterward, including persistence and readiness denial.
  Historical evidence without terminal status keeps its prior exited semantics.
- Fresh isolated ARM check store/adapter slice: 16 passed. This covers a real
  subprocess plus injected terminal observation, not a one-hour production timeout.
- Candidate Engineering foundation authored descriptions and UI states have explicit
  RU/EN bindings; names, paths and server diagnostic payloads remain unchanged.
  Root reviewed mobile rendering and independently passed the integrated Chrome
  context/foundation RU-to-EN workflow on desktop/mobile, with user-data preservation.
- No production runner/web restart or model-engine change in this slice. The complete
  engineering rollout and its long-running acceptance remain open.

### Approved check profiles: public owner workflow

- Added interactive-owner GET/POST check-profile routes on the existing checks
  store. Exact command approval uses revision checks; saving does not execute
  commands or grant trusted-host access. No client-supplied result is accepted.
- Profile listing uses project-scoped keyset pages without a total record cap.
  Fresh isolated ARM route/store slice: 18 passed, including 103 profiles,
  foreign-project cursors, owner/origin/internal-token denial and stale revision.
- Expanded integrated backend regression after the capability change: 54 passed
  and 3 subtests (routes, check store/runner, operations and model probes).
- UI integration is in progress. Durable background launch, run progress/artifact
  UI, clean verification copies and the full checks acceptance remain pending;
  these profile routes alone do not complete engineering stage 5.

### Durable check dispatch integrated with the existing operation manager

- Check launch now queues an owner-scoped, idempotent operation bound to the
  approved project/profile revisions; the HTTP queue request never contacts a host.
  The existing manager invokes the existing check runner and observes its result.
- Read-only run observation never dispatches commands. Cancellation/app shutdown
  stops observation, not an already dispatched host process; this distinction is
  explicit in the public check operation scope. An interrupted operation is not
  automatically re-executed. Stable run identity permits later evidence recovery.
- Adapter rechecks live operation authorization and revisions after awaited
  digests and immediately before command dispatch. Changed approvals fail closed.
- Fresh integrated ARM regression: 47 passed. Includes real temporary subprocess
  execution through saved queue and manager replacement; app stop after dispatch
  then read-only recovery records one side effect, not two. This is not a full
  machine reboot or production release test. Local operations unittest: 9 passed.
- Check execution/progress UI, artifacts, resource scheduling and clean verifier
  workspaces remain open. Production web/runner not switched by this slice.

### Approved-command profile UI verified

- Existing Engineering panel now exposes feature-gated profile selection, full
  command preview, explicit save approval, revision-aware editing and manual pages.
  No execution is claimed or dispatched by this form. Project rights are unchanged.
- Root independently passed the actual Chrome workflow on desktop/mobile: 56
  profiles, paging, raw data preservation across RU/EN, project-switch races,
  409 draft retention and explicit fresh-revision review beyond page one.
- Reviewed mobile screenshot; authored labels are Russian while commands, hashes,
  identifiers and user profile names stay literal. Syntax/diff checks pass.
- This completes the profile-management UI slice, not launch/progress controls,
  the overall engineering plan or deployment acceptance.

### Check output and process stop backend

- Owner-scoped output reads bounded runner byte pages by stable check identity,
  not arbitrary job IDs. Read/stop first reconcile without command dispatch.
- Separate explicit process-stop endpoint persists stop intent before sending the
  signal. Cancelled terminal evidence cannot pass even with exit code zero.
  Queue cancellation still does not imply stopping a previously dispatched job.
- Added additive stop-intent column; queued/finished check events are recorded in
  the existing project journal. Duplicate terminal observation emits one finish.
- Fresh isolated ARM integrated suite: 49 passed, including a real printf/sleep
  process, output pages, explicit stop, persistent cancellation, owner and request
  boundary checks. Production migration/rollback acceptance is still required.
- Launch/progress/log/stop UI is being integrated and is not claimed accepted yet.
  No production deployment or model-engine changes in this slice.
- Root source review found overlapping output polls could duplicate a byte page;
  candidate UI now fences generations/in-flight polls. The follow-up browser run
  failed earlier while waiting for ambiguous-launch status, despite one earlier
  workflow pass. This instability is under diagnosis; no final UI pass/release claim.

### Project-scoped check history and browser fixture diagnosis

- Operations history supports owner-validated project filtering and rejects
  cursors belonging to another project. A regression with 55 newer unrelated
  operations finds the older project's active check. Local operations: 10 passed;
  initial ARM operations/routes/queue slice: 24 passed.
- The ambiguous-launch browser failure was traced to Chromium automatically
  retrying a POST after the fixture destroyed its socket; both requests used the
  same UUID and launch succeeded. It was not evidence of a disappearing notice.
  The fixture now uses a deterministic HTTP 503 to exercise application recovery;
  the duplicate-output race remains a separate actual UI fix under final validation.

### Launch, observation and stop UI independently verified

- Root independently passed the actual Chrome desktop/mobile workflow on the
  frozen candidate: explicit launch, same UUID retry, reload observation, UTF-8
  output, operation cancellation distinct from host stop, project-switch fencing.
- Delayed-output refresh test now releases the old identical GET after the new
  observation begins (Chromium coalesces identical pending reads), then verifies
  exactly one appended output chunk. In-flight/generation fences prevent duplicates.
- Mobile screenshot reviewed: operation cancelled/check running are separately
  translated, and host stop remains an explicit action. No production rollout.
- Engineering API responses now carry no-store, including mapped auth/errors;
  fresh route/operations regression: 21 passed. Project-filter/old-active UI
  history wiring is the next bounded slice, not yet included in this browser pass.

### Project history UI and owner-approved readiness criteria

- Root independently passed updated Chrome desktop/mobile workflow: history is
  project-filtered, an older active operation beyond 50 recent records is recovered
  separately, explicit selection survives refresh, all 56 project records can be
  paged without duplicate display despite 55 unrelated records.
- Added owner-only, explicitly confirmed requirement API and paginated listing on
  existing check criteria storage. Criteria changes emit project journal events;
  optimistic versions prevent silently replacing another device's edit.
- Readiness endpoint obtains workspace hash from authenticated runner; client
  hash claims are ignored. Missing/stale checks cannot report ready. Initial full
  affected ARM slice: 51 passed. Additional 105-criterion pagination test added.
- Criteria/readiness UI is being integrated. This is evidence for the current
  check subsystem, not completed clean verifier copies, baseline comparison UI,
  the full engineering plan or production release. No deployment occurred.

### Versioned readiness snapshots and real public API check

- Readiness now identifies its exact criteria/profile/run/code snapshot with a
  deterministic hash and observation timestamp. Rewording criteria changes the
  snapshot; changing approved commands or code blocks reuse of old successful checks.
- Fresh ARM store/routes/queue slice: 26 passed. Added real subprocess-to-public
  API test: successful check -> ready, source-file change -> not ready/new hash,
  with only one command dispatch. Focused queue/API suite: 4 passed.
- This is a snapshot, not a promise that files cannot change after observation.
  Criteria UI must invalidate its displayed snapshot on local edits and label the
  returned code identity; that UI work is still in progress, not yet accepted.

### Criteria and readiness UI slice accepted in candidate

- Root independently passed frozen foundation behavior + actual Chrome criteria
  workflow on desktop/mobile. Explicit edits/mandatory changes, 409 draft retention,
  fresh revision review, 56-record pagination, raw data preservation and RU/EN pass.
- Readiness is manually observed and labelled as a specific workspace snapshot;
  local edits invalidate displayed evidence and late responses from another project
  are rejected. Root reviewed the mobile rendered snapshot and limitations.
- Syntax and diff checks pass. This completes only the criteria/readiness UI
  integration, not clean verification workspaces, full baseline comparisons,
  remaining engineering stages, final two-hour acceptance or Jetson deployment.

### Baseline comparison backend

- Added owner-scoped comparison of saved baseline/check outcomes, with command,
  profile, host, policy revision and reported-toolchain compatibility checks.
  Interrupted/stale/missing evidence is not comparable. This classifies command
  transitions only; repeated failure is not claimed to be the same individual bug.
- Check history now supports stable keyset pagination without a total run cap.
  Fresh ARM affected suite: 33 passed, including 105 records and public API
  comparison of real baseline/final subprocess runs, with no read-triggered execution.
- Baseline comparison UI is in progress. Structured test-case report comparison,
  complete environment identity and clean verification workspaces remain unverified
  or incomplete; no claim that this closes the full stage 5 or release.

### Baseline comparison UI independently verified

- Root ran the frozen real Chrome scenario independently: PASS, desktop/mobile,
  RU/EN, all five outcomes, history beyond 50 runs, failed requests and stale
  cross-project responses. Rendered mobile screenshot reviewed; names/toolchain
  text remain literal and no command is dispatched by comparison.
- Fresh ARM store/routes/queue suite: 34 passed. Added explicit rejection of
  changed reported environment and a baseline dated after the compared check.
- Artifact directory: `/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-baseline-independent-bm_72zhw`.
- Clean-copy audit confirmed current checks still execute in project.root.
  A bounded runner copy RPC is being implemented, preserving source/index and
  rejecting uncertain copies; adapter integration and acceptance remain pending.
  No production restart, deployment or full-release claim.

### Runner-backed verification copies integrated in candidate

- New checks/baselines persist copy preparation intent alongside their dispatch
  outbox. Runner copies the bounded source-v1 file set into a private ordinary
  directory, verifies source/copy SHA-256 and rechecks source before spawning.
  Adapter persists the confirmed cwd before dispatch and rechecks live permission.
  Existing dispatch rows keep their original cwd for compatible recovery.
- No Git commands, filters, hardlinks or automatic deletion are used during copy.
  Copy commands cannot accidentally discover a parent Git repository. Source and
  copy changes, symlinks/special files, uncertain preparations and revoked access
  fail closed. Lost acknowledgements recover the same copy/job identity; read-only
  observation never prepares a copy or executes a command.
- Root's frozen integrated ARM run: **89 passed, 8 subtests passed**, covering
  existing runner, copy, adapter, queued operations, hosts, checks and public routes.
  Real modifying checks leave the original code unchanged and become stale;
  lost copy acknowledgement plus adapter restart produces one command effect.
  Syntax and diff checks also pass.
- Limits remain explicit: ordinary copies are NOT sandboxes; approved absolute
  paths can still affect the trusted host. Git-history-dependent checks are not
  supported by this copy; source-v1 excludes Git/cache metadata, refuses more than
  10000 entries/128 MiB, and retains at most 128 copies pending operator review.
  Managed retention UI, richer project manifests, complete toolchain identity and
  final migration/rollback rehearsal remain required before full release.
- No production service was changed or restarted in this slice.

### Fresh menu audit and visible verification-copy limits

- Independent full authored-menu Chrome fixture passed for Russian/English,
  per-owner preference persistence, failed saves, desktop/mobile and preservation
  of user/model/code text. Five dynamic/document menu unit tests also pass.
- Manual screenshot inspection caught a gap outside the old menu selector:
  15 visibility-setting explanations remained English. Added explicit translations
  and included authored `.vis-hint` nodes in discovery. Fresh browser regression
  requires all 15 explanations in Russian and preserves literal `/settings` code.
  Final fixture PASS; mobile screenshot reviewed at
  `/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-menu-hints-final-q8edisd0`.
- Check launch panel now discloses in RU/EN that copies are not sandboxes, omit
  Git history, and block unsupported sizes rather than falling back to source.
  Actual Chrome launch/recovery/stop workflow with these disclosures passed at
  `/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-copy-ui-hxt5nrjj`.
- Syntax/diff checks pass. This is candidate/UI evidence, not a complete audit of
  every advanced description or completion of pending engineering stages. No deploy.

### Browser MCP version no longer floats

- Built-in startup and recovery guidance now share `@playwright/mcp@0.0.80`
  through `src/browser_runtime.py`, instead of `@latest`. Official upstream
  package.json and npm metadata agree on version and exact Playwright dependency
  `1.63.0-alpha-2026-08-31`; this is not a claim of full browser compatibility.
- Fixed cache detection: an installed package with the right name but wrong exact
  version no longer satisfies the local-cache check. Existing tag/range behavior
  is preserved for unrelated callers.
- Fresh isolated ARM tests: 17 passed for builtin startup/cache/Python path/MCP
  manager. Actual local `npx -y @playwright/mcp@0.0.80 --version` exited zero and
  reported Version 0.0.80. Syntax/diff checks pass.
- Browser tool handshake, navigation, per-task contexts, preview and trace
  acceptance remain pending. No production deployment or browser-runtime release.

### Real pinned browser MCP protocol smoke

- Added opt-in `tests/test_browser_runtime_smoke.py`: npm-offline cached package,
  real Chrome, owned loopback fixture and two simultaneous stdio MCP processes.
  Initialize/tools-list/navigation/explicit snapshot/invalid-argument/close pass;
  cookies and localStorage remain separate across those two processes.
- Initial fixture assumed navigation returned inline DOM. Actual 0.0.80 returns
  a snapshot file link; inspecting its implementation showed explicit
  `browser_snapshot` returns inline content. Updated the test to exercise that
  supported sequence, without claiming navigation itself provides inline DOM.
- Root real run: 1 passed in 1.357s. New test artifacts use a disposable cwd,
  not the user's project. Package runs without network installation.
- This proves package/protocol behavior, NOT Odysseus per-worker allocation,
  manager integration, internal-network policy, preview or trace acceptance.
  Those remain required; no deployment or complete browser-stage claim.

### MCP lost-reply replay defect fixed

- Deterministic regression reproduced two submissions from one manager call:
  `_do_call` failed after its side effect, reconnect succeeded, and the manager
  automatically dispatched the action again. This affected built-in browser,
  email and other potentially mutating tools, not only harmless reads.
- Manager now reconnects built-ins for future requests without replaying the
  uncertain invocation. It returns `outcome_unknown=true`, `retryable=false` and
  explicit guidance to inspect the result. Failed reconnection retains that
  classification. Remote exception text is not echoed by this failure path.
- Fresh isolated ARM regression: 18 passed (manager, reconnect metadata, cache,
  background startup). Two new tests first failed for duplicate effects / raised
  reconnect errors and passed after the fix; diff check passes.
- This closes automatic replay inside McpManager, not durable cross-process
  deduplication of all model-requested actions or full Team/browser integration.
  Those broader acceptance gates and production deployment remain pending.

### Team preserves uncertain MCP evidence

- Team MCP normalization previously dropped manager `outcome_unknown` metadata.
  It now retains uncertainty and `retryable=false`; timeout/invalid result follow
  the same path. Contradictory success/output flags cannot turn an uncertain
  response into verified evidence; remote error strings are not echoed.
- Fresh isolated ARM manager/Team adapter/runtime tests: 31 passed and 12
  subtests passed, including one dispatch only, uncertainty propagation, invalid
  result, timeout and existing owner/schema/revocation policies. Diff check passes.
- This passes information to the runtime; a durable gate against a model issuing
  a new equivalent action still requires integration with operation identities.
  No production change and no whole-plan completion claim.

### Durable unknown-result gate

- Tool result persistence now records explicit `outcome_unknown` as `unknown`,
  not `done`. Before creating any new intent, the store rejects a worker with
  unresolved unknown outcomes, including nominally read-only calls. A new model
  call ID cannot bypass that gate. Existing human reconciliation remains the
  release path; other workers retain independent work.
- Worker runtime checkpoints the tool response then exits this path rather than
  requesting another model step. `_unknown` includes explicit read uncertainty,
  so reassignment/stop handling cannot silently discard that state.
- Real SQLite reopen/reconcile tests pass for read and effectful intents.
  Fresh isolated ARM store + Team MCP/runtime regression: 66 passed, 14 subtests.
- A full model-driven unknown-result scenario, resource accounting of uncertain
  reads and final UI/recovery acceptance still need verification. No deployment.

### Unknown-outcome resource and native-batch integration

- Reproduced premature backend-slot release for explicit unknown read results;
  resource occupancy and configured task concurrency now include all unknown
  outcomes. Independent backends remain available; reconciliation releases the
  held slot. Real SQLite test initially failed and now passes.
- Actual Team loop test with model/MCP boundary fixtures exposed that generic
  failure handling left workers running: finish_worker correctly refused open
  uncertainty. Added a lease-fenced blocked transition without declaring success
  or failure of the underlying action. Checkpoint closes skipped native calls
  with not_executed results, preserving tool-call/result pairing for recovery.
- Fresh ARM: 68 passed, 14 subtests. The new loop scenario proves one model
  response, one remote call, blocked worker, unknown ledger and a complete native
  batch in checkpoint. Server-canonical call IDs are checked against saved calls.
- Real LLM and UI reconciliation/resume acceptance still pending; no deployment.

### Reconciliation reaches resumed worker context

- Resumed worker consumes durable tool_reconciled events through an owner-scoped
  exact-intent lookup. Human observations enter as bounded untrusted evidence,
  not permissions, and do not increment the successful-tool counter. No full
  intent-history scan is needed for each event.
- Extended actual Team loop fixture: unknown first call -> blocked -> human
  reconciliation -> new verification call -> done. Saved native batch remains
  paired; old remote call is not repeated and the human observation reaches the
  next model request. Only the new verified tool counts as success.
- Fresh ARM store/runtime: 48 passed, 2 subtests. Added foreign-owner denial for
  intent lookup; local store regression 41 passed. No production change. Real LLM,
  UI reconciliation and full release acceptance remain outstanding.

### Russian reconciliation controls verified

- Translated the uncertain-action explanation, title, validation errors and
  confirmation prompts. Tool identities/payload/evidence remain literal data;
  dynamic title uses a separate translated label rather than translating names.
- Actual Chrome Team control workflow passed before and after adding Russian
  assertions. RU action without evidence/confirmation produces the localized
  error and no resolve request; switching back to EN and completing explicit
  reconciliation passes the existing owner/session-isolation workflow.
- Final artifact directory:
  `/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-reconcile-ru-final-n53cvs0x`.
  JS syntax/diff checks pass. This is controlled API/browser evidence, not real
  LLM or production reconciliation acceptance; full plan/deployment remain open.

### Combined Team regression on current candidate tree

- Synced current src/routes/tests/static/scripts/app/services/core to isolated
  ARM candidate source, mounted read-only into the no-network test container.
  `python -m pytest -q tests/test_team*.py`: **235 passed, 1 skipped, 31 subtests**,
  31.90 seconds. This covers all 19 Team test files as collected; optional real
  browser layer is skipped there and has separate local Chrome evidence above.
- Fresh production inspection: `odysseus:jetson-engineering-20260914-bridge2`,
  healthy. No production restart or data change occurred.
- This broader regression supports combined compatibility of the current Team
  changes. It does not complete pending engineering stages, real multi-host/model
  acceptance, final duration test, migration rehearsal or deployment.

### Context preset drafts

- Added balanced/long-task/compact presets to the existing context policy panel.
  Selection previews changes only; explicit draft application checks the same
  overrides, and only Save persists through the existing versioned policy API.
  Context window/output reserve remain unchanged; quality/GPU guarantees are
  explicitly disclaimed. No model or compaction call is triggered by selection.
- Actual Chrome policy workflow passes preview/no POST, draft/discard by reload,
  explicit saved preset, existing conflict/validation gates, RU/EN and mobile.
  Artifact directory: `/var/folders/8w/gd34tdw52cdfk0cl0059c0fm0000gn/T/odysseus-context-presets-final-kgyhgtb9`.
- Custom saved presets/import, task/worker selection and remaining compaction
  requirements are still incomplete. No production deployment.
# 2026-09-14 scoped hotfix deployed (not full roadmap acceptance)

Production now uses `odysseus:jetson-engineering-20260914-hotfix`, image
`sha256:5250c5d9dadfcfa2bfc34b2e2bf6eb21a5d9034af4c2e875725dc32cb348dbdc`.
Includes replay round separation, Basic/Advanced Team setup, strict saved-plan
participant validation, context retention fixes and additional Russian settings,
Team labels and translated settings search. Search includes localized panel names;
ephemeral save/error notices are translated without restoring cleared messages.

Latest snapshot: `/home/xopmc/services/odysseus-qa-hotfix-jm4zyG`.
Targeted ARM tests: 58 passed, 4 subtests; local localization 8 passed and both
settings JS harnesses passed. Earlier broader candidate regression: 6534 passed,
33 skipped (predates the final small changes; not a new full-release acceptance).
Migration/rollback rehearsal passed on private copied data at
`/home/xopmc/services/odysseus-migration-rehearsal-iqyfjpwt`.
Authenticated active-chat and database task checks passed before restart.
Stopped-web SQLite backups and prior override are preserved at
`/home/xopmc/services/odysseus-hotfix-backup-1789384412642560084`.
Only application image changed; runner, engine and feature permissions unchanged.
Health recovered. Rollback procedure retains new databases rather than overwriting
new chats. No claim of complete Russian coverage, full live-render parity or
completion of the entire engineering roadmap; those acceptance gaps remain open.

### 2026-09-14 isolated-check and browser runtime hotfix

- Production now uses `odysseus:jetson-engineering-20260914-isolation-ui4`.
  The prior image and compose override remain under `/home/xopmc/services/` for rollback.
- A disposable isolated project ran an approved check through web container,
  authenticated runner, verification copy and Docker sandbox. It passed; recorded
  evidence confirms `sandbox.command.start`, network `none`, read-only root and
  the 512 MiB memory cap. No user project, chat or task was modified.
- The smoke revealed and fixed two wiring defects: the transport allow-list omitted
  `sandbox.command.start`, and the runner rejected server-generated check bindings.
  Those two bindings are fixed metadata only; images, mounts, network settings and
  arbitrary Docker arguments remain rejected.
- Browser MCP had an unwritable npm cache. Its cache is now pinned to
  `/tmp/odysseus-npm-cache`; after restart the log confirms the built-in browser
  MCP connected with 30 tools. Service health is `healthy`.
- Local runner regressions passed: 22 tests across host-runner, verification-copy
  and capability suites. This does not claim completion of the remaining roadmap.

### 2026-09-14 full clean ARM regression

- A complete `git archive` snapshot of the released source was tested on Jetson
  in a fresh no-network container with no production database mounted:
  **6542 passed, 33 skipped, 105 subtests passed** in 393.96 seconds.
- The first historical candidate directory was intentionally rejected by the
  harness because it was not a full source snapshot. The final result above
  comes from the complete tracked source, including launcher and MCP modules;
  it is the authoritative regression evidence for `isolation-ui6`.
