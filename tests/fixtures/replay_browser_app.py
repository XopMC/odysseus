"""Opt-in, content-free live replay fixture for browser QA on an isolated port.

Run only with a temporary ODYSSEUS_DATA_DIR and AUTH_ENABLED=false. It never
calls a model or a host tool. The synthetic producer remains active so a second
browser can exercise the newest-first attach and backward replay cursor.
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

if os.getenv("ODYSSEUS_ENABLE_REPLAY_QA") != "1":
    raise RuntimeError("Replay browser QA fixture requires explicit opt-in")

from app import app  # noqa: E402
from core.models import ChatMessage  # noqa: E402
from src import agent_runs  # noqa: E402


SESSION_ID = "00000000-0000-4000-8000-000000000205"
_original_lifespan = app.router.lifespan_context


async def _synthetic_run():
    event_count = min(100000, max(1, int(os.getenv("ODYSSEUS_REPLAY_QA_EVENTS", "205"))))
    delay_seconds = max(0.0, min(0.1, float(os.getenv("ODYSSEUS_REPLAY_QA_DELAY_MS", "0")) / 1000.0))
    for seq in range(event_count):
        round_number = seq // 5 + 1
        tool_id = f"fixture-tool-{round_number}"
        phase = seq % 5
        if phase == 0:
            data = {"type": "agent_step", "round": round_number}
        elif phase == 1:
            data = {"delta": "[thinking fixture]", "thinking": True, "round": round_number}
        elif phase == 2:
            data = {"type": "tool_start", "tool": "fixture_tool", "tool_call_id": tool_id,
                    "round": round_number}
        elif phase == 3:
            data = {"type": "tool_output", "tool": "fixture_tool", "tool_call_id": tool_id,
                    "exit_code": 0, "round": round_number}
        else:
            data = {"delta": "[answer fixture]", "round": round_number}
        yield "data: " + json.dumps(data, separators=(",", ":")) + "\n\n"
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
    await asyncio.Event().wait()


async def _stalled_model_run():
    """A real detached run task waiting on a model-like boundary, no provider I/O."""
    await asyncio.Event().wait()
    if False:  # pragma: no cover - makes this an async generator
        yield ""


@asynccontextmanager
async def _qa_lifespan(instance):
    async with _original_lifespan(instance):
        manager = instance.state.session_manager
        try:
            session = manager.get_session(SESSION_ID)
        except KeyError:
            session = manager.create_session(
                SESSION_ID, "SAFE live replay cursor QA", "http://127.0.0.1:1/v1", "fixture-model",
            )
            for index in range(50):
                session.add_message(ChatMessage("assistant", f"[synthetic prior message {index + 1}]"))
            manager.save_sessions()
        if os.getenv("ODYSSEUS_WAIT_QA_STALLED") == "1":
            run = agent_runs.start(SESSION_ID, _stalled_model_run(), initial_model="fixture-model")
            run.progress.started_at = time.time() - 700
            run.wait.phase_since = time.time() - 700
            run.wait.endpoint_id = "fixture-endpoint"
            agent_runs._publish(run, 'data: {"type":"agent_step","round":1}\n\n')
        elif os.getenv("ODYSSEUS_REPLAY_QA_START_RUN", "1") == "1":
            agent_runs.start(SESSION_ID, _synthetic_run(), initial_model="fixture-model")
        yield


app.router.lifespan_context = _qa_lifespan
