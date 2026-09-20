"""Agent tools for harness-efficiency artifacts."""

from __future__ import annotations

import json

from src.observation_pack import recall


class ReadToolArtifactTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict):
                raise ValueError
            oid = str(args.get("id") or "")
            offset = int(args.get("offset") or 0)
            chunk = recall(ctx.get("owner"), ctx.get("session_id"), oid, offset)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return {"error": "Stored tool observation is unavailable", "exit_code": 1}
        header = (
            f"[tool_artifact id={chunk['id']} offset={chunk['offset']} "
            f"next_offset={chunk['next_offset']} eof={str(chunk['eof']).lower()}]"
        )
        return {"output": header + "\n" + chunk["text"], "exit_code": 0, **chunk}


class PublishSubagentEvidenceTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.subagent_evidence import publish
        try:
            args = json.loads(content or "{}")
            state = ctx.get("subagent_state") or {}
            child_id = str(state.get("child_run_id") or "")
            if not child_id or not isinstance(args, dict):
                raise ValueError
        except (TypeError, ValueError):
            return {"error": "Evidence can only be published from an active child agent", "exit_code": 1}
        return publish(
            ctx.get("owner"), ctx.get("session_id"), child_id,
            kind=args.get("kind"), body=args.get("body"),
            artifact_refs=args.get("artifact_refs") or [],
        )


class ManageAutoResearchLabTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import auto_research_lab as lab
        try:
            args = json.loads(content or "{}")
            action = str(args.get("action") or "list")
        except (TypeError, ValueError, AttributeError):
            return {"error": "Auto-Research arguments must be an object", "exit_code": 1}
        if action == "list":
            return lab.list_experiments(ctx.get("owner"))
        if action == "create":
            return lab.create(
                ctx.get("owner"), ctx.get("session_id"),
                baseline_sha=args.get("baseline_sha"),
                candidate_worktree=args.get("candidate_worktree"),
                gates=args.get("gates") or [], objectives=args.get("objectives") or [],
            )
        if action == "record":
            return lab.record(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                candidate_sha=args.get("candidate_sha"), split=args.get("split"),
                metrics=args.get("metrics") or {}, evidence=args.get("evidence") or [],
            )
        if action == "propose":
            return lab.propose(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                candidate_sha=args.get("candidate_sha"), parent_sha=args.get("parent_sha"),
                hypothesis=args.get("hypothesis"), patch_ref=args.get("patch_ref") or "",
            )
        if action == "select_pareto":
            return lab.select_pareto(ctx.get("owner"), str(args.get("experiment_id") or ""))
        if action == "candidates":
            return lab.candidates(ctx.get("owner"), str(args.get("experiment_id") or ""))
        if action == "claim":
            return lab.claim(ctx.get("owner"), str(args.get("experiment_id") or ""),
                             str(args.get("candidate_id") or ""), str(args.get("split") or ""))
        if action in {"pause", "resume", "close"}:
            return lab.set_status(ctx.get("owner"), str(args.get("experiment_id") or ""),
                                  {"pause": "paused", "resume": "open", "close": "closed"}[action])
        if action == "evaluate":
            return lab.evaluate(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                str(args.get("candidate_sha") or ""),
            )
        return {"error": "Unknown Auto-Research action", "exit_code": 1}
