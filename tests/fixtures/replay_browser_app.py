"""Opt-in, content-free live replay fixture for browser QA on an isolated port.

Run only with a temporary ODYSSEUS_DATA_DIR and AUTH_ENABLED=false. It never
calls a model or a host tool. The synthetic producer remains active so a second
browser can exercise the newest-first attach and backward replay cursor.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

if os.getenv("ODYSSEUS_ENABLE_REPLAY_QA") != "1":
    raise RuntimeError("Replay browser QA fixture requires explicit opt-in")

from app import app  # noqa: E402
from core.models import ChatMessage  # noqa: E402
from src import agent_runs  # noqa: E402


SESSION_ID = "00000000-0000-4000-8000-000000000205"
_original_lifespan = app.router.lifespan_context


async def _synthetic_run():
    for seq in range(205):
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
    await asyncio.Event().wait()


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
        agent_runs.start(SESSION_ID, _synthetic_run(), initial_model="fixture-model")
        yield


app.router.lifespan_context = _qa_lifespan
