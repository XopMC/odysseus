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

## 18:35 onward: 128K and child-lifecycle release verified live

- Final code/Git main+master/Jetson checkout: `c90b415a70cb949862a951f67db2f24bbf9f90db`,
  production `release-c90b415`. Local full suite **7,494 passed, 25 skipped,
  115 subtests**; exact isolated Jetson image **185 passed**. Alternate-port
  HTTP 200; before app-only switch both parent and child active counts were zero.
  App/teams backups `*-pre-c90b415.db` passed integrity checks, mode 0600;
  host QA workspace archived separately. Runtime source SHA256s match checkout.
- Actual Safari Settings → Agent tools: changed the existing saved 32,768 value
  to **131,072** using clicks/paste/Tab. UI value and persisted settings.json
  matched. This was a user-requested edit, not a silent default migration.
- Resumed safe QA as attempt 5, run `efa7044ab9ff422aa092dc0a44c1fa1f`.
  Actual context telemetry recorded configured ceiling **131,072**, effective
  generation budget **90,955**, window **131,840**, input occupancy **33,831**.
  The physical window cap is explicit; the old 32K generation cap is gone.
- Reloaded Safari and independently authenticated Opera during this run;
  both showed the active Goal/Plan and current tools/thinking, and matched
  **31.2% / 41,190 tokens** after synchronizing. No new duplicate run.
- Parent autonomously created children 4 (:4) and 5 (:5) at 18:03:50, ~0.1s
  apart, while continuing its own work. Both completed. Child 5 had 24 distinct
  tool calls, 23 checkpoints and a compaction; this was not a thinking loop.
- Entered additional safe read-only review guidance through Safari. It was
  persisted and reached the working checkpoint. Parent delegated child 6 (:3)
  with an actual recorded **timeout_seconds=5** (tool-start seq 643).
  Child `b560edc702cb4d69b59f6dd915045993` ran **18:40:48–18:44:29 UTC** and
  completed: 11 tool starts/results, 7 checkpoints, 3,388 result characters.
  This directly verifies useful work survives the legacy five-second task limit.
- Removed **213** unused versioned Odysseus image tags after checking each
  commit exists and no container uses its image. Kept production and rollback
  `release-5ac5593`, all databases/backups and non-matching image tags. No global
  Docker prune or network/VPN/Wi-Fi modification was performed.
- One external read-only diagnostic using SQLite's five-second default busy
  timeout reported `database is locked`. Production log aggregate had zero
  corresponding lock errors/tracebacks/HTTP 500; next read took 2ms and health
  6.6ms. Database uses DELETE journal mode. Investigate contention; this is not
  proof of a failed model run, nor proof of sustained contention acceptance.
- Same-worker child question-resume now uses the exact committed ledger and
  fences obsolete task callbacks (regression covered). Live question-resume,
  process-restart recovery, remaining full-Goal audit and final six-hour
  interactive soak are still OPEN. Do not mark the Goal complete.

Additional open observations for the next audit slice:

- Actual context popup shows 75% of usable input and 37.3% effective full-window
  trigger, consistent with the saved 65,536 input cap and 131,840 serving window.
  Its separate "Messages" row still counts canonical turns (3), while the
  header counts rendered rounds. Clarify or unify that row; don't call this a
  frozen header counter or change the saved cap silently.
- Final child-6 panel was opened by real Safari click: it explicitly showed
  completed, with 0 active / 6 total children. Parent remained active.
- Production start timestamp: **2026-09-27 18:34:20 UTC**. Post-release HTTP and
  HTTPS health both 200, zero restarts; point sample CPU 16.32%, RSS 1.274 GiB.
- The old automation id `odysseus-safe-6h-soak` view tool renders an app card but
  exposes no saved fields; `$CODEX_HOME` is unset and the conventional automation
  directory is absent here. No new minute probe or replacement automation was
  started in this slice. Do not infer continuous six-hour monitoring from older
  probe logs or stale heartbeat prompts. The active Codex Goal remains open.

## 19:01–19:12 UTC — live result-read defect and replay recheck

- Real Safari QA reload retained current activity; header advanced 230 → 238.
  Independently authenticated Opera reload recovered the same current rounds,
  count 239 → 243. Scrolling older rounds and clicking their thinking disclosure
  loaded the saved 23:57 reasoning (287 tokens), not an empty card.
- Live parent repeatedly tried to retrieve child 6's already-completed result,
  then incorrectly tried send_to_session. The child stored 3,388 result characters
  after 81,384 characters of metrics. The generic tool formatter truncated its
  structured extras at 8,000 characters and falsely labelled the record
  "Session created". The result itself was durable, but invisible to the model.
- Deterministic baseline formatter reproduction: result absent, 8,135 formatted
  characters, false session-created label. Patched result-first read: result
  present, 417 formatted characters, no false label. Added bounded Unicode-safe
  result paging, explicit recovery-context opt-in, owner-scope forwarding and
  invalid-input tests. REST/UI full child record stays unchanged.
- QA Goal independently reached completed/attempt 5/revision 19. No active
  durable runs remained at the check. Independently ran its actual host project
  tests: **85 tests, OK**, 0.875 seconds, exit 0. This validates that project test
  suite only, not the whole harness or all project acceptance requirements.
- SQLite contention local reproduction: a held 6-second DELETE-journal writer
  made an external 5-second reader fail while the application's 30-second reader
  succeeded. This is not proof of an app failure. Do not enable WAL casually.
- Audit additionally reproduced backup corruption of snapshot semantics: a live
  WAL copied beside a staged DB replayed a later transaction during restore.
  Backup now excludes staged DB sidecars and fails closed on backup errors;
  deterministic snapshot/later-commit/restore and handle-cleanup tests pass.
  Production journal mode, network, VPN and Wi-Fi remain unchanged.
- Focused child suites: **71 passed**. Combined backup/result/delivery: **30
  passed**. Fresh full suite: **7,510 passed, 25 skipped, 115 subtests**, exit 0,
  216.66 seconds; JS syntax and diff checks green. Exact candidate, release and
  post-release model read still pending at this entry. No acceptance completion
  is claimed.

## 19:15 UTC release and newly exposed live-thinking buffering

- Published exact `c41a6b9a2192c1a6995ef71559dffcec3f4d8aac` to public-fork
  main/master and Jetson checkout. Isolated no-network candidate **83 passed**;
  alternate-port login redirect followed to HTTP 200. Verified online app/teams
  backups `*-pre-c41a6b9.db`, confirmed zero active runs, switched app only.
  Production started 19:15:41 UTC. During ~54-second startup HTTP was 000/HTTPS
  502; after application startup both 200, Docker healthy, zero restarts.
  Runtime changed-file SHA256s match checkout. Candidate container removed.
- Real Safari sent a read-only child-result verification prompt in the existing
  safe QA chat. Run `0317ba40d958480c86d9e9e23fac6d65` actually invoked
  manage_subagents; its 3,388-character tool output exactly equals the durable
  child-6 result (exit 0). No child re-execution occurred. Thus result delivery
  was exercised by the real model, not just a unit test.
- User reported 17K generated tokens while the UI still showed no tokens.
  Fallback adapter buffered all candidate thinking until ordinary content or a
  complete tool call. Durable replay later received **17,391 thinking frames /
  69,664 characters**. Aggregated 8-word shingles were 99.3% unique, maximum
  repeated shingle count 7; this does not establish a mechanical repetition
  loop (nor prove semantic usefulness). No thinking text needed for this check.
- Correction to an initial hypothesis: the provider repetition guard is BELOW
  this buffer and was not disabled. The defect is live visibility, delayed
  persistence and availability to downstream consumers, not absence of that
  guard. Explicitly stopped only this QA request via Safari; journal preserved,
  status stopped with matching live/durable cursor 17,585.
- New regression uses an async barrier: first thinking must arrive before
  final content is permitted. It failed on c41a6b9 (timeout), passed after the
  single-candidate streaming fix. Thinking-only output still produces an error,
  not success; no duplicate reasoning on errors. Cancellation closes provider.
  Multiple-candidate fallback attribution behavior is deliberately unchanged.
- Additional OPEN finding: after restart the ordinary request emitted
  workspace_rejected for the host-only `/tmp/odysseus-long-qa-20260927`; route
  validation checked container filesystem. This read-only check did not mutate
  files. Fix needs trusted-host-aware validation without bypassing owner/path
  authorization. New ordinary turns also reassembled ~190K estimated tokens
  before compaction; checkpoint reuse after Goal completion needs investigation.
- Thinking candidate keeps single-route model attribution, authoritative usage
  and finish reason even when the provider subsequently fails or emits no usable
  answer. Review caught the existing terminal-usage drop; regression added.
  Affected transport suites: **162 passed**. First full pass before the final
  usage/cancellation tests: 7,513 passed; final exact-state full pass is running.

## 19:36–19:41 UTC — exact final slice and real streaming acceptance

- Exact local/public-fork main/master/Jetson checkout/image code:
  `7e703661cf544f6997bcf1fe14241ec6b23b4b8f`, `odysseus:release-7e70366`.
  Final full suite **7,516 passed, 25 skipped, 115 subtests**, 214.11s, exit 0.
  Exact isolated Jetson image **173 passed**; alternate-port HTTP 200.
  Runtime `src/llm_core.py` SHA256 equals checkout:
  `ffa9d183e6527182bbb312c2901b8c960cde3a6fa80fbbbbf0e42773a085b76c`.
- Production started **19:36:07.927 UTC**, application startup completed at
  19:37:02. Verified fresh app/teams online backups `*-pre-7e70366.db`, mode0600,
  zero-active-run gate before switching. HTTP/HTTPS200, Docker healthy/0restarts.
- Created harmless arithmetic chat through actual Safari clicks/input:
  `f56ba884-9dec-4c00-97f1-73de6c3866e7`, selected Qwen3.8 base model (:2 unused).
  Run `5d975576beab467780f001ffcf1bee88`: observed **running**, durable seq605,
  thinking already in journal, no final-text event. Safari visibly showed an
  actively expanding thinking block (601 estimated tokens /21.9s).
- Reloaded Safari during thinking and navigated independently authenticated
  Opera to that same chat. Opera visibly showed continuing thinking before
  final output (1,775 estimated tokens /62.2s); two messages, not a frozen one.
  No cookie copying, extra model requests or tool/host permission expansion.
- Finished normally: status done, live/durable cursor **5,111**. Journal contains
  5,053 thinking frames, 42 text frames, zero tool calls. First thinking server
  timestamp 1790537872.4010158; first final text 1790537949.8887832 — **77.49s
  of reasoning was observable before the final response**, unlike old buffering.
- Final answer `397077185014` with residues17/29/43/71 matches an independent
  Python CRT calculation. Both browsers reloaded to the same final answer and
  10.8% /14,197 backend tokens. Clicking the saved thinking disclosure in Opera
  loaded its retained body successfully (77.5s/2,171 estimated thinking tokens).
- Post-startup log counts: zero SQLite-lock messages, Tracebacks, HTTP500 lines;
  active durable runs0. Removed only candidate tags7e70366/c41a6b9 and older
  release tags c90b415/5ac5593 after validating container references. Current
  release and c41a6b9 rollback retained; no DB/backups/network changes.
- Six-hour continuous final-image acceptance is STILL OPEN. No new persistent
  minute probe has been started; do not mistake these real point checks for
  six continuous hours. Restart child recovery, trusted-host workspace routing,
  ordinary-turn checkpoint reuse and heavy child-list metrics remain open.

## Next slice — host workspace survives container replacement

- Previous Goal turn was progress: released/tested actual live thinking and
  child result delivery, while leaving broad acceptance open.
- Reproduced selected host-only path rejected by container `vet_workspace`.
  Added a private read-only workspace-info operation to the fixed SSH helper;
  only the registered host owner after the existing admin gate can use it.
  Picker browse/vet, send-time binding and explicit file-path inference now
  validate on the execution host. No model-advertised tool or privilege added.
- Selected cwd propagates to foreground Python/shell, file/checkpoint tools and
  encoded background requests. Local SSH wrappers start in a valid local package
  directory, not a nonexistent host path inside Docker. Regression exposed this
  dispatch distinction before release. No global host default is overwritten.
- Exact approval uses its sealed workspace, not a mutable composer path; Deny
  works when host validation is offline. Revalidation failure before effectful
  dispatch blocks without consuming the exact approval. Local-owner paths keep
  local validation; no host-to-container fallback on validation failure.
- Updated workspace help to avoid claiming trusted-host tools are sandboxed by
  the selected folder. Root/sensitive binds, wrong-owner lookup, helper failure,
  host-file parent inference, real-helper directory lookup and background cwd
  regressions covered. Focused **236 passed**; full final pass running.
- Earlier full pass: 7,528 passed/1 failed. Failure was the comparison tool's
  prior assertion that host dispatch drops workspace (`{}`); updated it to
  require the selected cwd. Did not weaken owner or execution-host assertions.
- Separate context audit confirms ordinary turns after completed Goal bypass
  working-ledger restore. A safe fix needs an owner-scoped exact-run anchor AND
  transcript-edit validation; simply reusing the latest checkpoint can restore
  deleted/edited messages. Still open, not mixed into this workspace patch.
- Isolated candidate uncovered a clean-image issue missed by the warm Mac
  checkout: the default agent data working directory may not exist yet. The
  transport wrapper now starts from its installed package directory; only its
  JSON request uses the host cwd. First candidate: 101 passed/1 failed; never
  deployed. Keep this failure as evidence, not a flaky retry.
