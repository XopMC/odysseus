# Six-hour safe QA acceptance (2026-10-06 local date)

## Scope and clock

- User authorized a new safe long-running chat, same primary model, child reliability/KV budgets and unnecessary Full Access confirmations.
- QA chat: `abe059c4-1ac3-48c5-a6fd-e236032bed54`; workspace `/tmp/odysseus-qa-20261006` only.
- Main model: `rvn-qwen3.8-flash-next-q4km-strata` at `192.168.50.4:49285` (same route as original chat; metadata only inspected).
- Goal first persisted active: **2026-10-05 21:04:49 UTC**, attempt 1. Earliest six-hour active-observation boundary: **2026-10-06 03:04:49 UTC**. Any image switch starts a fresh final-image window.
- Original chat must not be read, resumed or steered. Jetson VPN/Wi-Fi/routes are outside scope.
- No acceptance claim yet. Tests, fixes, release and active-load/browser parity evidence remain required.

## Initial observations

- Exact production image: `odysseus:release-5f24749`, healthy, zero restarts.
- Safari real click/paste/Return created the new chat; Goal active, Plan created and file tools running. Shell on, web search off, Full Access already selected.
- Read-only serving metadata: main llama.cpp `/slots` has one slot `n_ctx=262144`.
- LM Studio selected exact Qwen instances base/:2/:3/:4 each report loaded `context_length=131072`, `parallel=2`. Unified-cache mode is not reported. Do not assert every backend divides context identically; use conservative capacity until authoritative slot metadata proves otherwise.
- Existing child gate allows four child calls and does not include foreground parent traffic in its semaphore. Loaded concurrency and KV sharing are ignored by context discovery. This is a confirmed scheduling/budget defect; the cause of a specific HTTP 5xx still needs live evidence.
- Recent historical failed child metadata reports HTTP 504 after ten retries and earlier external-outage HTTP 503. No fresh provider error body was read from the old chat.
- QA prompt asks for base/:3/:4, avoids :2, nonoverlapping modules, parent independent work, 150+ tests, 1M-row generator/reference, subprocess fault injection, backup/restore and iterative independent reviews. No network/secrets/system settings.

## In progress (not acceptance)

- Minute availability probe live exec handle **94068**, first sample **2026-10-05 21:07:08 UTC**. The discarded first setup handle 41174 checked the wrong `/health` URL (302); corrected probe uses public `/api/health`. Samples 1–12 returned HTTP/HTTPS 200. This pre-release window is not final-image acceptance.
- Heartbeat `odysseus-safe-qa-6h-kv-reliability` active every 15 minutes. Poll the actual live handle, not only stored output.
- Local loaded-instance/shared-request fixes: 30 focused tests passed. Full Access interaction/approval safety shard: 215 passed; a full pytest is still running, so do not claim green yet.
- At 21:16 UTC parent live/durable cursor matched 12750/12750, Goal remained active. Four children had real activity; the allocator selected :2 for the fourth despite the prompt preference (all four are selected globally). No child route was changed by code yet; avoid assuming natural-language exclusions are authoritative routing constraints.
- One :3 child had a genuine HTTP-shaped **504 caused by ReadTimeout**, not a verified provider HTTP 500. It had zero streamed chars and restarted from its initial checkpoint, retry count 1. The configured per-request idle timeout was 300 seconds. A separate bounded eight-token diagnostic request to :3 is in exec handle 39081; do not repeat it while pending.
- Safari reload during active Goal restored Plan 1/11, Stop and current 34.1% live context; server cursor continued advancing. Opera was launched into a new QA tab but requires owner sign-in; an asynchronous login request was issued, no cookies/password copied.

## Pre-release failure evidence / safe pause

- Probe 94068 samples 13–14 and 21–27 returned curl 000/exit 28 for both HTTP and HTTPS; this is **not** a continuous passing soak. During overlapping periods model POST connects to both :1234 and :49285 produced ConnectTimeout and retryable 503. Do not attribute these to KV overflow without evidence. Fresh host/container root probes later returned 200 and internal app health was 200 in 0.01s. Docker remained healthy/zero restarts. Conntrack 1685/262144, no shown interface error counters; no networking mutation made.
- Bounded :3 eight-token diagnostic timed out at 40.01s with no HTTP status. The :3 worker later reported 400 and subsequent 503; raw task contents were never inspected. Other children had real tool starts/outputs/checkpoints, so large thinking counts alone are not evidence of a loop.
- First full run: 7800 passed, 32 failed because `/usr/bin/git` refuses the Xcode EULA, 27 skipped. No license accepted. Re-run with existing standalone Git on process-local PATH: **7835 passed / 25 skipped / 115 subtests**. A later local timeout helper's missing import was caught and repaired by the focused shard (**153 passed**); a fresh final full run is pending in handle 18446, log `/tmp/odysseus-qa-20261006-final-pytest.log`.
- Safari pause clicks initially appeared ineffective while requests were unavailable/Opera focus had changed. After explicitly raising the Safari window, pause succeeded: Goal paused, revision 8, attempt 4 at approximately 21:34 UTC; no active parent runs and no pending tool intents. Do not claim this was a repaired product UI defect.
- Children continued independently; host QA workspace contained 13 files. Preserve it and child checkpoints across the app-only image switch. The existing owner setting contains legacy 300s provider idle timeout; the patch imposes a 600s minimum only for local Agent requests while respecting longer values/cloud choices.
