# Team workspace

The team feature is **off by default** (`ODYSSEUS_TEAM_ENABLED=0`). It adds an
owner-scoped API and UI beside the existing single-agent chat. Enabling this
flag is a deployment decision, not a claim that the complete acceptance matrix
has passed. Current live evidence belongs in the release verification report.

## Operating model

- Select a lead and a pool of endpoint/model pairs; enable automatic planning,
  or assign bounded objectives manually. Workers have separate checkpoints.
- A worker's `done` is a proposal for acceptance, not completion of the team.
  Review and integration precede configured build/test commands and the final
  report. Git source checkouts are not silently replaced by the integration tree.
- Local endpoints with the same physical backend share a resource group. Local
  capacity is one; different hosts can run concurrently. Model pools and plans
  have no fixed participant count ceiling, and there is no team-wide/global
  model request cap. Backend capacities, budgets and write locks still apply.
  Explicit store callers may opt into a concurrency ceiling. Third-party callers outside Odysseus are not governed by this
  scheduler. Configure `ODYSSEUS_TEAM_RESOURCE_ALIASES` for additional aliases.
- External routes require task consent, explicit data scope, known input/output
  prices and a shared budget. `goal_only` permits planning; executor/reviewer
  contexts need `assigned_context`. Keys stay in the existing server endpoint
  configuration. Conservative reservations are estimates, not provider invoices.
- Connection establishment retries at most three times on the same endpoint.
  A sent request or partial stream is not automatically replayed. Reassignment
  is restricted to the manually selected task pool.
- A cancelled scope cannot be reopened. Create a new worker/task. Paused tasks
  retain their checkpoints; an uncertain command outcome requires inspection
  and explicit reconciliation before continuation.

## Host and project access

The daemon runs as the configured trusted Unix user, independently of the web
container. It uses a private Unix socket reached via the existing pinned SSH
identity. PTYs survive browser/web-process disconnects, but **not host/daemon
restarts**. Interrupted commands are not relaunched. Output is capped per job
and old output is pruned without discarding idempotency records.

The runner is **not a sandbox for hostile model-generated code**. File tools
enforce scopes and protect common credential paths; shell access retains the
trusted Unix account's capabilities. In particular, membership in `docker` is
root-equivalent. The command guard catches common accidental privileged,
destructive and publishing actions, not every possible shell-language bypass.
Use the existing separate `/host-access` approval page for sudo; do not place
passwords in a chat, task instruction or terminal transcript.

Git workers use isolated worktrees derived from the permitted dirty source
state, without modifying its index. Integration requires exact reviewed tree
versions and detects conflicts. File and Git rollback refuse a divergent
current version rather than overwriting a user's later edits. Dedicated file
writes have bounded checkpoints; arbitrary shell programs are not automatically
transactional. Run risky scripts in a Git worktree or take an explicit backup.

Project profiles contain path, constraints, install, run, build and test commands.
Install/run commands are instructions, not automatically executed when a profile
is loaded. Build/test commands are the final acceptance checks.
Loading a profile does not grant host, web or external-provider permission.
Research results retain tool evidence; a model's unsupported claim is not proof
that it read a source. Browser notifications require a user gesture and an open
team page; these are not background push notifications.

## Jetson deployment and rollback gate

1. Keep the current production image, compose override, source and a consistent
   SQLite backup. Do not copy a staging database over production data.
2. Build `Dockerfile.team` on the verified ARM image with only source assets in
   the build context. Run the new build against copied data and an alternate
   loopback port, feature enabled there only.
3. Verify owner/permission/budget boundaries, dirty-source Git integration,
   intentional conflicts, PTY recovery and output limits, two-client replay,
   real independent backends, legacy regressions, and the required two-hour
   compaction scenario. Paid provider tests require separate permission.
4. Install `host_runner.py`, `host_runner_client.py`, `host_files.py` and
   `team_tool_paths.py` under `~/services/odysseus-host`. Install the supplied
   user service under `~/.config/systemd/user`, reload and enable it. Ensure
   user lingering is enabled. Do not run two daemons against the same state.
5. Set the verified image and host runner client path in the existing compose
   override, keeping port 5130, volumes and every unrelated setting. Enable the
   team flag only after the gate is complete. Verify login and a fresh task.
6. On a regression, disable the feature and restore the previous image/override.
   The new `data/teams.db` is separate from `app.db`; leave it for diagnosis and
   recovery. Do not restore an old `app.db` over newer chats. Inspect and stop
   team commands separately: reverting the web image does not kill the runner.

`teams.db` uses an additive versioned schema, durable event sequence numbers,
worker/coordinator leases, checkpoints and an effect-intent ledger. Its backup
must use SQLite's backup mechanism or a quiescent database, not an isolated
copy of the main file while WAL writes are active.
