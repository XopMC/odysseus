"""model_interaction_tools.py - agent tools for talking to other models.

Owns the model-interaction tool implementations (chat_with_model, ask_teacher,
list_models) and their handler classes, registered in ``TOOL_HANDLERS``. Part
of the tool -> registry migration (#3629): the implementations were moved here
out of ``src.ai_interaction`` so dispatch flows through the registry instead of
the elif chain / dispatch_ai_tool in tool_execution.py.

Shared helpers that still live in ``src.ai_interaction`` and are used by tools
not yet migrated (``_resolve_model``, ``AI_CHAT_TIMEOUT``) are imported lazily
inside the functions to avoid an import cycle at module load.
"""
import asyncio
import json
import logging
import uuid
from typing import Dict, Optional

logger = logging.getLogger(__name__)


_TEACHER_SYSTEM_PROMPT = (
    "You are a senior AI mentor. A less capable model is stuck on a problem and asking for help. "
    "Provide clear, actionable guidance:\n"
    "1. Brief analysis of the problem\n"
    "2. Recommended approach (step by step)\n"
    "3. Key things to watch out for\n\n"
    "Be concise and practical. No preamble."
)


async def chat_with_model(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Send a message to a specific model and return its response.

    Content format:
      Line 1: model_name (or model_name@endpoint_name)
      Line 2+: the message to send
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async

    lines = content.strip().split("\n", 1)
    if not lines or not lines[0].strip():
        return {"error": "First line must be the model name"}

    model_spec = lines[0].strip()
    message = lines[1].strip() if len(lines) > 1 else ""
    if not message:
        return {"error": "No message provided (line 2+ is the message)"}

    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await llm_call_async(
            url, model,
            [{"role": "user", "content": message}],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        return {"model": model, "response": response}
    except Exception as e:
        logger.error(f"chat_with_model failed: {e}")
        return {
            "error": f"Failed to get response from {model_spec}: {e}",
            "untrusted_content": True,
        }


async def ask_teacher(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Ask a more capable model for help.

    Content format:
      Line 1: model_name (or 'auto')
      Line 2+: the problem description
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async
    from src.settings import get_setting

    lines = content.strip().split("\n", 1)
    model_spec = lines[0].strip() if lines else "auto"
    problem = lines[1].strip() if len(lines) > 1 else ""

    if not problem:
        return {"error": "No problem description provided"}

    if model_spec.lower() in ("auto", ""):
        model_spec = get_setting("teacher_model", "")
        if not model_spec:
            return {"error": "No teacher model configured. Specify a model name or set teacher_model in settings."}

    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await llm_call_async(
            url, model,
            [
                {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Problem:\n{problem}"},
            ],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        return {"model": model, "response": response, "teacher": True}
    except Exception as e:
        logger.error(f"ask_teacher failed: {e}")
        return {
            "error": f"Teacher call failed ({model_spec}): {e}",
            "untrusted_content": True,
        }


async def delegate_subagent(content: str, ctx: dict) -> Dict:
    """Start one real child agent and return immediately to the parent."""
    from src.settings import get_setting
    from src.ai_interaction import _resolve_model
    from src.subagent_runtime import runtime
    from src.subagent_limits import (
        DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        MAX_ACTIVE_PER_MODEL,
        MAX_ACTIVE_ON_PARENT_MODEL,
        MAX_SUBAGENT_TIMEOUT_SECONDS,
    )

    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return {"error": "Subagent arguments must be a JSON object", "exit_code": 1}
    if not isinstance(payload, dict):
        return {"error": "Subagent arguments must be a JSON object", "exit_code": 1}
    objective = str(payload.get("objective") or "").strip()
    assigned_context = str(payload.get("context") or "").strip()
    requested_model = str(payload.get("model") or "same").strip()
    if not objective or len(objective) > 20000 or len(assigned_context) > 100000:
        return {"error": "Subagent objective/context is missing or too large", "exit_code": 1}
    try:
        timeout_seconds = int(payload.get("timeout_seconds") or DEFAULT_SUBAGENT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return {"error": "Subagent timeout must be an integer", "exit_code": 1}
    if not 5 <= timeout_seconds <= MAX_SUBAGENT_TIMEOUT_SECONDS:
        return {"error": f"Subagent timeout must be between 5 and {MAX_SUBAGENT_TIMEOUT_SECONDS} seconds",
                "exit_code": 1}

    mode = str(get_setting("agent_subagents_mode", "off") or "off")
    if mode == "off":
        return {"error": "Subagents are disabled in Agent settings", "exit_code": 1,
                "policy": "disabled_by_policy"}
    allowed = get_setting("agent_subagent_models", "")
    if isinstance(allowed, str):
        allowed = [item.strip() for item in allowed.split(",") if item.strip()]
    elif isinstance(allowed, list):
        allowed = [str(item).strip() for item in allowed if str(item).strip()]
    else:
        allowed = []
    raw_model_limits = get_setting("agent_subagent_model_limits", {})
    model_limits = {}
    if isinstance(raw_model_limits, dict):
        for raw_spec, raw_limit in raw_model_limits.items():
            if not isinstance(raw_spec, str) or isinstance(raw_limit, bool):
                continue
            try:
                parsed_limit = int(raw_limit)
            except (TypeError, ValueError):
                continue
            if 1 <= parsed_limit <= MAX_ACTIVE_PER_MODEL:
                model_limits[raw_spec] = parsed_limit

    max_active_for_model = MAX_ACTIVE_PER_MODEL
    if mode == "same_model":
        # The account setting is authoritative.  Small/local models sometimes
        # hallucinate a provider alias (for example "sonnet") even after being
        # told to pass model="same".  In same-model mode that argument must not
        # be able to override or break the selected parent route.
        url = ctx.get("current_endpoint_url")
        model = ctx.get("current_model")
        headers = ctx.get("current_headers") or {}
        if not url or not model:
            return {"error": "Current model route is unavailable for a subagent", "exit_code": 1,
                    "policy": "unavailable_transport"}
        resolved_spec = requested_model
        max_active_for_model = MAX_ACTIVE_ON_PARENT_MODEL
    else:
        if not allowed:
            return {"error": "No subagent models are configured", "exit_code": 1,
                    "policy": "disabled_by_policy"}
        automatic = requested_model.lower() in {"", "same", "auto"}
        if not automatic and requested_model not in allowed:
            return {"error": "Requested subagent model is outside the configured allowlist", "exit_code": 1,
                    "policy": "disabled_by_policy"}

        state = ctx.get("subagent_state") if isinstance(ctx.get("subagent_state"), dict) else {}
        cache_key = (
            ctx.get("owner") or "", tuple(allowed),
            tuple(sorted(model_limits.items())),
            str(ctx.get("current_model") or ""),
            str(ctx.get("current_endpoint_url") or "").rstrip("/"),
        )
        cached = state.get("_resolved_model_pool")
        if not isinstance(cached, dict) or cached.get("key") != cache_key:
            resolved = await asyncio.gather(*(
                asyncio.to_thread(_resolve_model, spec, owner=ctx.get("owner"))
                for spec in allowed
            ), return_exceptions=True)
            pool = []
            for spec, item in zip(allowed, resolved):
                if isinstance(item, Exception):
                    continue
                candidate_url, candidate_model, candidate_headers = item
                endpoint_id = spec.rsplit("@", 1)[1] if "@" in spec else None
                is_parent = (
                    str(candidate_model) == str(ctx.get("current_model") or "")
                    and str(candidate_url).rstrip("/") == str(ctx.get("current_endpoint_url") or "").rstrip("/")
                )
                configured_capacity = model_limits.get(spec, MAX_ACTIVE_PER_MODEL)
                pool.append({
                    "spec": spec, "url": candidate_url, "model": candidate_model,
                    "headers": candidate_headers or {}, "endpoint_id": endpoint_id,
                    "capacity": (
                        min(configured_capacity, MAX_ACTIVE_ON_PARENT_MODEL)
                        if is_parent else configured_capacity
                    ),
                })
            cached = {"key": cache_key, "pool": pool}
            state["_resolved_model_pool"] = cached
        pool = list(cached.get("pool") or [])
        if not pool:
            return {"error": "No configured subagent model is currently available", "exit_code": 1,
                    "policy": "unavailable_transport"}

        # The LLM is not an authority for routing.  Older schemas exposed a
        # ``pin_model`` boolean and the model could falsely claim the user had
        # pinned a route, sending every child to one backend.  An exact model
        # is only a tie-break preference: active count remains the primary
        # sort key, so all selected routes are used before any route is reused.
        candidates = pool
        if not automatic:
            candidates = sorted(candidates, key=lambda candidate: candidate["spec"] != requested_model)

        ranked = []
        for order, candidate in enumerate(candidates):
            active = runtime.active_count(
                owner=ctx.get("owner"), endpoint_url=candidate["url"],
                model=candidate["model"], endpoint_id=candidate["endpoint_id"],
            )
            if active < int(candidate["capacity"]):
                ranked.append((active, order, candidate))
        if not ranked:
            return {"error": "All selected subagent models are at capacity", "exit_code": 1,
                    "policy": "model_capacity_exhausted"}
        _active, _order, selected = min(ranked, key=lambda item: (item[0], item[1]))
        resolved_spec = selected["spec"]
        url, model, headers = selected["url"], selected["model"], selected["headers"]
        max_active_for_model = int(selected["capacity"])

    return await runtime.spawn(
        owner=ctx.get("owner"), session_id=ctx.get("session_id"),
        parent_run_id=ctx.get("parent_run_id"), objective=objective,
        assigned_context=assigned_context, endpoint_url=url, model=model,
        headers=headers or {}, endpoint_id=(
            resolved_spec.rsplit("@", 1)[1]
            if mode != "same_model" and "@" in resolved_spec else None
        ),
        timeout_seconds=timeout_seconds, workspace=ctx.get("workspace"),
        access_mode=str(ctx.get("access_mode") or ""),
        disabled_tools=set(ctx.get("parent_disabled_tools") or []),
        tool_policy=ctx.get("parent_tool_policy"),
        allowed_tools=ctx.get("parent_allowed_tools"),
        external_untrusted_context_seen=bool(ctx.get("external_untrusted_context_seen")),
        delegated_credential=bool(ctx.get("delegated_credential")),
        max_active_for_model=max_active_for_model,
    )


async def manage_subagents(content: str, ctx: dict) -> Dict:
    """List, inspect, guide, stop, remove or join child agents."""
    from src.subagent_runtime import runtime
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return {"error": "Subagent management arguments must be JSON", "exit_code": 1}
    if not isinstance(payload, dict):
        return {"error": "Subagent management arguments must be an object", "exit_code": 1}
    action = str(payload.get("action") or "list").strip().lower()
    owner, session_id = ctx.get("owner"), ctx.get("session_id")
    child_id = str(payload.get("child_id") or "").strip()
    if action == "list":
        return {"subagents": runtime.list(owner, session_id), "exit_code": 0}
    if action in {"read", "view"}:
        row = runtime.get(owner, session_id, child_id)
        return ({**row, "exit_code": 0} if row else {"error": "Subagent not found", "exit_code": 1})
    if action == "message":
        return await runtime.message(owner, session_id, child_id, payload.get("message") or "")
    if action == "stop":
        return await runtime.stop(owner, session_id, child_id)
    if action in {"remove", "delete"}:
        return await runtime.remove(owner, session_id, child_id)
    if action == "wait":
        child_ids = payload.get("child_ids") or ([child_id] if child_id else [])
        if not isinstance(child_ids, list) or not child_ids:
            return {"error": "wait requires child_ids", "exit_code": 1}
        return await runtime.wait(
            owner, session_id, child_ids,
            timeout_seconds=payload.get("timeout_seconds", 600),
            wait_for=str(payload.get("wait_for") or "any"),
        )
    if action == "list_evidence":
        from src.subagent_evidence import list_evidence
        return list_evidence(owner, session_id, child_id=child_id)
    if action == "list_candidates":
        from src.subagent_evidence import list_candidates
        return list_candidates(owner, session_id)
    if action == "submit_candidate":
        from src.subagent_evidence import submit_candidate
        return submit_candidate(
            owner, session_id, child_id,
            title=payload.get("title"), payload=payload.get("payload") or {},
            evidence_ids=payload.get("evidence_ids") or [],
        )
    if action == "verify_candidate":
        from src.subagent_evidence import verify_candidate
        return verify_candidate(
            owner, session_id, str(payload.get("candidate_id") or ""),
            str(payload.get("verifier_child_id") or child_id),
            verdict=payload.get("verdict"), notes=payload.get("notes") or "",
        )
    return {"error": f"Unknown subagent action: {action}", "exit_code": 1}


async def list_models(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """List all available models across configured endpoints.

    Content = optional filter keyword.
    """
    import json
    import httpx
    from src.database import SessionLocal, ModelEndpoint
    from src.llm_core import _detect_provider, ANTHROPIC_MODELS
    from src.auth_helpers import owner_filter
    from src.endpoint_resolver import resolve_endpoint_runtime, build_headers, build_models_url

    keyword = content.strip().lower() if content.strip() else None

    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoints = query.all()
        if not endpoints:
            return {"results": "No enabled model endpoints configured."}

        result_lines = []
        total_models = 0

        for ep in endpoints:
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception:
                continue
            provider = _detect_provider(base)
            headers = build_headers(api_key, base)

            model_ids = []
            if provider == "anthropic":
                model_ids = list(ANTHROPIC_MODELS)
            else:
                try:
                    models_url = build_models_url(base)
                    if models_url:
                        r = httpx.get(models_url, headers=headers, timeout=5)
                        r.raise_for_status()
                        data = r.json()
                        model_ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                        if not model_ids:
                            model_ids = [
                                m.get("name") or m.get("model")
                                for m in (data.get("models") or [])
                                if m.get("name") or m.get("model")
                            ]
                    else:
                        model_ids = json.loads(ep.cached_models or "[]")
                except Exception:
                    model_ids = ["(endpoint offline)"]

            if keyword:
                model_ids = [m for m in model_ids if keyword in m.lower() or keyword in (ep.name or "").lower()]

            if model_ids:
                result_lines.append(f"\n**{ep.name or base}** ({provider}):")
                for mid in model_ids:
                    result_lines.append(f"  - `{mid}`")
                    total_models += 1

        if not result_lines:
            return {"results": "No models found" + (f" matching '{keyword}'" if keyword else "") + "."}

        header = f"Available models ({total_models} total):"
        return {"results": header + "\n".join(result_lines)}
    except Exception as e:
        logger.error(f"list_models failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Handler classes registered in TOOL_HANDLERS
# ---------------------------------------------------------------------------

class ChatWithModelTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await chat_with_model(content, ctx.get("session_id"), owner=ctx.get("owner"))


class AskTeacherTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await ask_teacher(content, ctx.get("session_id"), owner=ctx.get("owner"))


class DelegateSubagentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await delegate_subagent(content, ctx)


class ManageSubagentsTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await manage_subagents(content, ctx)


class ListModelsTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await list_models(content, ctx.get("session_id"), owner=ctx.get("owner"))
