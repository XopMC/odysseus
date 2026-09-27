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
