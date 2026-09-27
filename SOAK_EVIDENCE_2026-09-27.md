# Odysseus live acceptance — 2026-09-27

## 16:08–16:28 UTC: child recovery release

- Content-free production inspection confirmed HTTP 400/500 after completed
  child tools, one model-selected whole-run deadline and one compaction failure.
  No main-chat message bodies or task commands were opened.
- Published source commit `ab5a9573` plus documentation-placement correction
  `f616f374cd3843eaadb3c30620df243a84c1209b` to public-fork main/master.
- Final local full pytest: **7,468 passed, 25 skipped, 113 subtests**, exit 0.
  Static JS syntax checks: exit 0. Isolated no-network Jetson candidate: **93
  passed**. Candidate alternate-port HTTP health: 200.
- Confirmed no active Chat/Goal/child/Team work before switching. Online SQLite
  backups `backups/app-pre-f616f37.db` and `backups/teams-pre-f616f37.db` passed
  integrity checks, mode 0600. Prior image `release-02351dd` retained for rollback.
- Production `odysseus:release-f616f37`: HTTP/HTTPS :5130 = 200/200,
  Docker healthy, zero restarts. Startup log aggregate: no traceback, SQLite
  lock errors or ERROR records. Only the app container was recreated.
- A disposable alternate-port candidate was stopped. No VPN/Wi-Fi/routing,
  proxy or global host configuration was changed.

## 16:28 onward: real large QA project (in progress)

- Through real Safari clicks/paste/Enter created Goal chat
  `dcc12a48-6ca5-41ba-94e8-ea1f577f87a4`, run
  `31f999bb611745c4985f325b3b495195`, Qwen3.8 27b NVFP4 base model.
- Dedicated workspace `/tmp/odysseus-long-qa-20260927`. Initial directory creation
  was in the container, but live tool receipts and host file listing subsequently
  confirmed the inherited trusted-host executor writes to the **host** path.
  Preserve that host directory; verify both paths before any release. Service
  data directory was intentionally not selected as a workspace.
- Task: offline stdlib telemetry CLI, CSV/SQLite/statistics/export, 50+ tests,
  100,000-row independent reference dataset, measured benchmark, adversarial
  review, evidence report. Restricted to the QA directory; no network, secrets,
  installs, service or host changes. User model :2 excluded in the task.
- Safari and independently authenticated Opera attached to the same live run.
  Initial context matched 12.7% (16,695 / 131,840). Safari reload preserved Goal
  active attempt 1 and Stop control; screenshot verified a generating-response
  card, not an empty terminal history. No duplicate run was created.
- At 16:34:19 UTC, two child runs overlapped: `2bb76b159fc145f6aad22b0e81694666`
  on :3 and `8076ff4c4f004b998b834c46643278f3` on :4. Parent stayed running;
  durable cursor progressed 5 → 10,774 → 18,020. Child 1 had multiple successful
  tools and two independent context checkpoints while child 2 was pre-filling.
- Actual Safari floating Subagents button opened both children; child detail
  opened, then panel collapsed back to parent. Parent subsequently issued
  WRITE_FILE; context updated through 17.8%, 19.3%, then 28.7% backend measurement.
- A sample app load: CPU 19.76%, RSS 1.216 GiB. This is a single sample, not
  sustained performance acceptance or a six-hour result.

## Context defect identified; next patch not yet deployed

Production settings have `agent_input_token_hard_max=65536`, output reserve
32768 and no explicit context policy for the affected main chat. The Agent
legacy path subtracted output reserve from the input cap and still used 85%,
although the UI advertised default 75%. A content-free snapshot showed an
effective threshold 19.4%. Fix under local verification: enabled defaults use
the same policy as explicit profiles; legacy adapter uses ContextBudget; live
UI uses actual run threshold, including schemas, rather than a profile-only
preview. User-configured reserves/caps are not silently changed.

The long-run acceptance Goal remains OPEN. Completion, real network recovery,
compaction, final Plan parity, restart/deadline semantics and sustained load
remain to be verified. Do not label this early activity a successful full soak.

## User-observed failures during this run

- Opera/Safari reload showed `1 msg` and a generic generation card while the
  parent had completed many rounds. Inspection confirmed the session discovery
  mutex awaited the entire `resumeStream` lifetime; count polling never ran again.
- The 200-event replay tail represented only tokens of one long round. It did
  not represent the latest 50 visible messages/rounds. Full artifacts still
  existed; a bounded coalesced activity snapshot is under regression test.
- At 16:58–17:00 UTC child 1 had completed 9 distinct tool actions, created stats
  module/tests and published evidence. Child 2 had no tools or visible output,
  ~178K reasoning characters across 2 rounds and one identical long sentence
  repeated 225 times. This is a reproduced reasoning loop, not inferred merely
  from a high token count. Existing 96-token repetition detection missed long
  phrases and subword splits. A bounded reconstructed-thinking guard is under test.
- Child 3 is a later separate task; don't mislabel its prefill as a proved loop.
- User additionally requested default response/thinking cap **131072** with the
  editable settings field retained. Implement AFTER replay repairs; keep actual
  model window constraints explicit, and validate interaction with context
  reserves before changing live defaults. This requirement remains OPEN.

## 17:20–17:31 UTC: replay repair and a second failure discovered live

- `95dcf070e125272bc0bfcf76d6b81e7508aac4d9` passed local full pytest
  **7,480 passed, 25 skipped, 113 subtests**, JS syntax and real isolated Chromium
  active-run 10K-event replay. Exact Jetson candidate: **138 passed**.
- QA Goal was explicitly paused, stuck child 2 cancelled through Safari;
  children 1 and 3 completed. All durable parent/child runs were terminal before
  app-only switch. Online app/teams SQLite backups passed integrity checks;
  host QA workspace archived as `backups/qa-workspace-pre-95dcf07.tar.gz`.
- Production changed to `release-95dcf07` at 17:25 UTC. HTTP and HTTPS returned
  200 after startup. No networking/proxy configuration changes.
- Actual Safari reload still exposed only the final stopped round. This was
  NOT accepted as repaired history. Deeper shape checks showed 53-element round
  arrays contained 52 empty slots: `_persist_timeline_v2` had reconstructed them
  from its 5,000-event inline preview, then overwritten canonical metadata.
  Full durable event files remain intact. Previously noted "53 rounds" was an
  array length, not evidence of 53 non-empty restored cards.
- Follow-up `a54c599` reconstructs terminal rounds from a sequential scan of
  the full committed replay log; only inline timeline preview stays bounded.
  Regression with 5,100 late thinking frames retains early text, thinking and
  completed tool output; local and isolated Jetson focused suites **79 passed**.
  Full suite, rollout and actual history restoration verification still pending.
- Captured child 2 reasoning had 285 repeats of one long sentence and no tools.
  Revised bounded guard detects its real saved stream at character 114,903.
  This is distinct from legitimate long output by children 1 and 3. A synthetic
  guard pass alone was insufficient: its earlier shorter window missed this loop.

## 17:35–17:40 UTC: restored real history, then resume failure

- `a54c599ea9be2f7895bad514236a8d579df43f0d` full pytest **7,481 passed,
  25 skipped, 113 subtests**, focused Jetson 79 passed, candidate HTTP 200.
  Deployed only the app after zero-active-run gate and verified SQLite backups.
- Reprojected ONLY the safe QA assistant row from its existing 46,680-event
  durable log. Result: **53 non-empty round texts, 53 non-empty reasoning slots,
  53 tool cards**. No command/model execution occurred during recovery.
- Safari real reload displayed **54 msgs**, old rounds and tools. Independently
  authenticated Opera reload matched. Clicked the same old 22:07 thinking block
  in both browsers; both loaded the same content and **0.1s / 355 tok** artifact.
- Safari Resume created attempt 2 but failed context shaping before model work:
  old full-history request 70,943 estimated tokens; summary attempt 71,388.
  Goal moved to waiting_user with context_uncompactable. This is a FAILED resume,
  not an acceptance pass or a model-provider outage.
- Root causes: compaction event downgraded the durable ledger to summary-only
  until another tool completed; legacy summary restore changed `ctx.messages`
  but left actual `route_messages` on full history. Candidate now commits the
  complete post-compaction ledger immediately and restores both route views.
- Added regression exposed another checkpoint defect: summary/untrusted markers
  were discarded by ledger serialization. Corrected with a narrow provenance
  whitelist; permission metadata is explicitly not restored. Fresh focused
  recovery/context suite **76 passed**, final full-suite/candidate/live checks pending.

## Resume candidate verification

- Final candidate `5ac55939dfffae9de04a5b91e5d649496ddf2aa3`: local full pytest
  **7,483 passed, 25 skipped, 113 subtests**, exit 0; JS syntax/diff check clean.
  Exact isolated Jetson image: **76 passed**, alternate-port HTTP 200.
- Prior intermediate full run exposed the missing summary/provenance marker
  assertion (one failure); fixed the implementation rather than weakening the
  test, then reran the full suite. Intermediate `cf54757` was never production.
- Before switching: zero running parents/children. Separate app/teams SQLite
  backups `*-pre-5ac5593.db` passed integrity checks, permissions 0600.

## 17:48–17:54 UTC: live resume and two-browser recovery passed for this slice

- Production `release-5ac5593` started 17:48:13 UTC; healthy, zero restarts.
- Real Safari Resume continued the same QA Goal as attempt 3 / run
  `cd8a712f55e34bf0baf0f3c41ff25f78`. It remained active while executing actual
  READ_FILE, MANAGE_SUBAGENTS, PYTHON and UPDATE_PLAN_STEP tools. Model recovered
  from a stale step-ID error and advanced the saved Plan from 0/10 to 3/10.
- Durable cursor advanced 84 → 3,325 → 3,501; checkpoint ledger contained
  18, then 25 messages (not summary-only). No automatic success was inferred.
- Reloaded Safari DURING work, then independently authenticated Opera DURING
  work. Both recovered current tools/reasoning and remained attached to the
  active Goal. Opera screenshot showed **64 msgs**, **30.3%**, latest 22:53
  round; Safari later showed **32.9%** with continuing model work, not frozen 1.
- In Opera, native upward scroll loaded earlier 22:07–22:09 saved rounds while
  the model continued. Clicking Scroll to bottom returned to the live stream.
- Latest health probe responded HTTP 200 in 6.6 ms. Earlier paused sample CPU
  4.89%, RSS 1.231 GiB is a point sample, not a sustained performance guarantee.
- This verifies the reported disappearance/reconnect/resume slice. The larger
  Goal, 128K default request, child deadline/restart lifecycle and sustained
  final-image soak remain OPEN. Do not mark the entire project accepted.

## Next candidate: 128K ceiling and mini-goal lifetime

- Previous turn is progress: release, restored durable history, live Safari /
  Opera reload and working Goal continuation were directly verified.
- QA attempt 3 continues; durable cursor 6,885 → 11,230 → 13,518. Actual host
  test files changed while local checks ran; no user main chat was accessed.
- Default `agent_output_token_budget` is now 131,072 in code and editable UI.
  Stable context reservation remains separate; dispatch computes the ceiling
  from the exact route's available window, prompt, schemas and safety margin.
  Existing saved 32K preference still requires an explicit live settings edit
  AFTER the new image is deployed. It has not yet been changed on old production.
- Removed model-supplied aggregate child lifetime deadline. Legacy argument is
  explicitly deprecated, not silently presented as enforced. Model inactivity,
  individual tool timeouts, cancellation, policy gates and unknown-effect fences
  stay in place. Raw model transport timeout/network failures also enter ten
  retries only when no unsettled tool action exists.
- Tests cover a progressing child beyond two elapsed hours, long elapsed retry
  cycles, exact ten retries, explicit Stop retaining partial work, policy denial
  and unknown effects. Restart still fences old children; full automatic restart
  recovery remains a separate OPEN requirement.
- Initial full suite found a real small-window retry regression (borrowed output
  capacity incorrectly became next-round reserved space), now repaired. Four
  fallback assertions also encoded the old output-equals-reserve contract;
  changed them to assert exact per-route free-space ceilings AND unchanged
  compaction reserves, including truthful fallback telemetry.
- Fresh affected suite: **254 passed**. Final full-suite and Jetson candidate /
  actual Safari settings verification are pending; no deployment claimed yet.
