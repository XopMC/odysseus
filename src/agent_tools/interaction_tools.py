import json
import logging

logger = logging.getLogger(__name__)


def _scope(ctx):
    if not isinstance(ctx, dict):
        return None, None
    return ctx.get("owner"), ctx.get("session_id")


class CreatePlanTool:
    async def execute(self, content, ctx):
        try:
            data = json.loads((content or "{}").strip())
            owner, session_id = _scope(ctx)
            if not owner or not session_id:
                raise ValueError("create_plan requires an active owned chat")
            from src.chat_work_store import store
            plan = store.save_plan(
                owner, session_id, str(data.get("title") or "Plan"),
                data.get("steps") or [], expected_revision=data.get("expected_revision"),
            )
            return "create_plan", {"plan_update": plan, "output": "Plan saved for user approval.", "exit_code": 0}
        except Exception as exc:
            return "create_plan: invalid", {"error": str(exc), "exit_code": 1}

class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {"question": question, "options": options, "multi": multi},
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s)", desc, len(options), multi)
        return desc, result

class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: the agent writes back to the active plan — tick an item done
        or revise steps (e.g. when the user asks to change something). Pure UI
        marker: returns a `plan_update` payload the agent loop turns into a
        `plan_update` SSE event; the frontend replaces the stored plan and refreshes
        the docked plan window. Does NOT end the turn.
        """
        raw = (content or "").strip()
        plan = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("plan"):
            plan = str(parsed.get("plan", "")).strip()
        else:
            plan = raw

        if not plan:
            return "update_plan: invalid", {
                "error": "update_plan needs a non-empty `plan` (the full updated checklist as markdown).",
                "exit_code": 1,
            }

        plan = plan[:8192]
        done = plan.count("- [x]") + plan.count("- [X]")
        total = done + plan.count("- [ ]")
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        plan_update = {"plan": plan}
        owner, session_id = _scope(ctx)
        if owner and session_id:
            try:
                from src.chat_work_store import store
                current = store.get(owner, session_id).get("plan")
                saved = store.save_plan(
                    owner, session_id, (current or {}).get("title") or "Plan", plan,
                    expected_revision=(current or {}).get("revision", 0),
                )
                # ``save_plan`` preserves an already executing/done status and
                # reconciles the existing step IDs. Calling plan_action here
                # would try to execute an already executing plan and turn a
                # valid progress update into a conflict.
                plan_update = saved
            except Exception as exc:
                return "update_plan: failed", {"error": str(exc), "exit_code": 1}
        result = {
            "plan_update": plan_update,
            "output": f"Plan updated ({done}/{total} steps complete)." if total else "Plan updated.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s", desc)
        return desc, result


class UpdatePlanStepTool:
    async def execute(self, content, ctx):
        try:
            data = json.loads((content or "{}").strip())
            owner, session_id = _scope(ctx)
            if not owner or not session_id:
                raise ValueError("update_plan_step requires an active owned chat")
            from src.chat_work_store import store
            plan = store.update_plan_step(
                owner, session_id, str(data.get("step_id") or ""),
                str(data.get("status") or ""), summary=str(data.get("summary") or ""),
                expected_revision=data.get("expected_revision"),
            )
            return "update_plan_step", {"plan_update": plan, "output": "Plan step updated.", "exit_code": 0}
        except Exception as exc:
            return "update_plan_step: failed", {"error": str(exc), "exit_code": 1}


class GetGoalTool:
    async def execute(self, content, ctx):
        owner, session_id = _scope(ctx)
        try:
            if not owner or not session_id:
                raise ValueError("get_goal requires an active owned chat")
            from src.chat_work_store import store
            goal = store.get(owner, session_id).get("goal")
            if not goal or goal.get("status") in {"completed", "cancelled"}:
                raise ValueError("No active goal")
            return "get_goal", {"goal_update": goal, "output": json.dumps(goal, ensure_ascii=False), "exit_code": 0}
        except Exception as exc:
            return "get_goal: failed", {"error": str(exc), "exit_code": 1}


class UpdateGoalProgressTool:
    async def execute(self, content, ctx):
        try:
            data = json.loads((content or "{}").strip())
            owner, session_id = _scope(ctx)
            if not owner or not session_id:
                raise ValueError("update_goal_progress requires an active owned chat")
            from src.chat_work_store import store
            goal = store.update_goal(
                owner, session_id, data.get("progress") or "",
                data.get("checkpoint") or {}, waiting_user=bool(data.get("waiting_user")),
            )
            return "update_goal_progress", {"goal_update": goal, "output": "Goal checkpoint saved.", "exit_code": 0}
        except Exception as exc:
            return "update_goal_progress: failed", {"error": str(exc), "exit_code": 1}


class CompleteGoalTool:
    async def execute(self, content, ctx):
        try:
            data = json.loads((content or "{}").strip())
            owner, session_id = _scope(ctx)
            if not owner or not session_id:
                raise ValueError("complete_goal requires an active owned chat")
            from src.chat_work_store import store
            goal = store.complete_goal(owner, session_id, data.get("summary") or "", data.get("evidence") or [])
            return "complete_goal", {"goal_update": goal, "goal_completed": True, "output": "Goal completed with verification evidence.", "exit_code": 0}
        except Exception as exc:
            return "complete_goal: failed", {"error": str(exc), "exit_code": 1}
