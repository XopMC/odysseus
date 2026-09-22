"""Agent tools for harness-efficiency artifacts."""

from __future__ import annotations

import json
import asyncio
import time

from src.observation_pack import recall, search


_research_drivers: dict[str, asyncio.Task] = {}


async def _drive_research(owner, experiment_id: str, ctx: dict) -> None:
    """Durable queue driver; workers are independent real subagents."""
    from src import auto_research_lab as lab
    from src.agent_tools.model_interaction_tools import delegate_subagent
    started = time.monotonic()
    failures = 0
    while time.monotonic() - started < 7200:
        lab.recover_expired_work(owner, experiment_id)
        snapshot = lab.workflow_snapshot(owner, experiment_id)
        if snapshot.get("error") or snapshot.get("status") != "open":
            return
        stages = snapshot.get("stages") or []
        queued = [item for item in stages if item.get("status") == "queued"]
        running = [item for item in stages if item.get("status") == "running"]
        if not queued and not running:
            trained = any(item.get("stage") == "train" and item.get("status") == "completed"
                          for item in stages)
            heldout_started = any(item.get("stage") == "heldout" for item in stages)
            if trained and not heldout_started:
                selection = lab.select_pareto(owner, experiment_id)
                if selection.get("selected"):
                    continue
            return
        if not queued:
            await asyncio.sleep(1.5); continue
        claimed = lab.claim_work(owner, experiment_id, worker_id="research-driver",
                                 event_id=queued[0]["event_id"], lease_seconds=900)
        work = claimed.get("work")
        if not work:
            await asyncio.sleep(.5); continue
        role = work["actor_role"]
        if work.get("stage") == "heldout":
            try:
                from src.auto_research_isolation import run_heldout
                output = await run_heldout(owner, experiment_id, event_id=work["event_id"],
                                           lease_token=claimed["lease_token"])
                result = lab.submit_work(owner, experiment_id, event_id=work["event_id"],
                                         lease_token=claimed["lease_token"], output=output)
                if result.get("exit_code") != 0:
                    raise RuntimeError(result.get("error") or "Held-out result was rejected")
                failures = 0
            except Exception as exc:
                lab.release_work(owner, experiment_id, event_id=work["event_id"],
                                 lease_token=claimed["lease_token"], reason=str(exc)[:1000])
                failures += 1
                if failures >= 3:
                    return
                await asyncio.sleep(2)
            continue
        assignment = dict(work.get("input") or {})
        if work.get("sealed_assignment"):
            assignment["sealed_environment"] = work["sealed_assignment"]
        objective = (
            f"You are the independent {role} worker for blind Auto-Research experiment {experiment_id}. "
            f"Execute only stage {work['stage']} iteration {work['iteration']}. Input: "
            f"{json.dumps(assignment, ensure_ascii=False)}. Use the project tools and verifier evidence. "
            "Do not inspect other workflow stages, train results, or held-out results. "
            "When finished, call manage_auto_research_lab exactly once with action=submit_work, "
            f"experiment_id={experiment_id}, event_id={work['event_id']}, "
            f"lease_token={claimed['lease_token']}, outcome=completed, and a structured output object. "
            "A proposal needs parent_sha and hypothesis; candidate_sha is optional until implementation. "
            "An implementation must commit its work and return candidate_sha. Reviewer verdict must be "
            "accept, revise, or reject. Validation outputs must contain numeric metrics."
        )
        spawn = await delegate_subagent(json.dumps({
            "objective": objective, "context": "Blind role-scoped assignment. Never request hidden held-out feedback.",
            "model": "auto", "timeout_seconds": 850,
        }), ctx)
        if spawn.get("error"):
            lab.release_work(owner, experiment_id, event_id=work["event_id"],
                             lease_token=claimed["lease_token"], reason=spawn["error"])
            failures += 1
            if failures >= 3:
                return
            await asyncio.sleep(2)
        else:
            failures = 0
            await asyncio.sleep(.25)


async def recover_research_drivers() -> int:
    """Resume durable open workflows after a web-process restart."""
    from src.settings import get_setting
    if not bool(get_setting("auto_research_lab_enabled", False)):
        return 0
    from core.database import AutoResearchExperiment, AutoResearchStageEvent, Session, SessionLocal
    from routes.prefs_routes import get_access_mode_for_user
    db = SessionLocal()
    try:
        experiments = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.status == "open",
        ).all()
        resumable = []
        for exp in experiments:
            unfinished = db.query(AutoResearchStageEvent).filter(
                AutoResearchStageEvent.experiment_id == exp.id,
                AutoResearchStageEvent.status.in_(["queued", "running"]),
            ).count()
            session = db.query(Session).filter(Session.id == exp.session_id).first() if exp.session_id else None
            if unfinished and session:
                resumable.append((exp.id, exp.owner, exp.candidate_worktree, session.id,
                                  session.endpoint_url, session.model, dict(session.headers or {})))
    finally:
        db.close()
    started = 0
    for experiment_id, owner, workspace, session_id, endpoint_url, model, headers in resumable:
        prior = _research_drivers.get(experiment_id)
        if prior and not prior.done():
            continue
        ctx = {
            "owner": owner or None, "session_id": session_id, "workspace": workspace,
            "current_endpoint_url": endpoint_url, "current_model": model,
            "current_headers": headers, "access_mode": get_access_mode_for_user(owner or None),
            "subagent_state": {"started": 0},
        }
        _research_drivers[experiment_id] = asyncio.create_task(
            _drive_research(owner or None, experiment_id, ctx),
            name=f"auto-research-recover-{experiment_id[:8]}",
        )
        started += 1
    return started


class ReadToolArtifactTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict):
                raise ValueError
            oid = str(args.get("id") or "")
            offset = int(args.get("offset") or 0)
            chunk = recall(ctx.get("owner"), ctx.get("session_id"), oid, offset,
                           run_id=ctx.get("parent_run_id"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return {"error": "Stored tool observation is unavailable", "exit_code": 1}
        header = (
            f"[tool_artifact id={chunk['id']} offset={chunk['offset']} "
            f"next_offset={chunk['next_offset']} eof={str(chunk['eof']).lower()}]"
        )
        return {"output": header + "\n" + chunk["text"], "exit_code": 0, **chunk}


class SearchArtifactsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        run_id = ctx.get("parent_run_id")
        if not run_id or not ctx.get("session_id"):
            return {"error": "Artifact search requires an active run and chat",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict) or set(args) - {"query", "limit", "cursor"}:
                raise ValueError
            page = search(ctx.get("owner"), ctx["session_id"], run_id,
                          args.get("query"), limit=args.get("limit", 10),
                          cursor=args.get("cursor"))
        except (OSError, TypeError, ValueError):
            return {"error": "Artifact search is unavailable or arguments are invalid",
                    "code": "invalid_arguments", "exit_code": 1}
        return {"output": json.dumps(page, ensure_ascii=False), "exit_code": 0, **page}


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
        subagent = isinstance(ctx.get("subagent_state"), dict) and bool(
            (ctx.get("subagent_state") or {}).get("child_run_id")
        )
        if subagent and action != "submit_work":
            return {"error": "Role workers cannot inspect or control the global research workflow", "exit_code": 1}
        if action == "list":
            return lab.list_experiments(ctx.get("owner"))
        if action == "create":
            from src.auto_research_isolation import validate_git
            runner_host_id = str(args.get("runner_host_id") or "")
            baseline_sha = str(args.get("baseline_sha") or "").lower()
            candidate_worktree = str(args.get("candidate_worktree") or "")
            try:
                await validate_git(ctx.get("owner"), runner_host_id, candidate_worktree,
                                   baseline_sha, scope="auto-research-create")
            except Exception:
                return {"error": "Execution host could not verify a clean exact baseline", "exit_code": 1}
            return lab.create(
                ctx.get("owner"), ctx.get("session_id"),
                baseline_sha=baseline_sha, candidate_worktree=candidate_worktree,
                gates=args.get("gates") or [], objectives=args.get("objectives") or [],
                runner_host_id=runner_host_id, validated_baseline_sha=baseline_sha,
            )
        if action == "configure_environment":
            from src.auto_research_isolation import experiment_host, validate_environment
            experiment_id = str(args.get("experiment_id") or "")
            split = str(args.get("split") or "")
            root = str(args.get("root") or "")
            manifest = args.get("manifest") or {}
            runner_host_id = str(manifest.get("runner_host_id") or args.get("runner_host_id") or "")
            if not runner_host_id:
                try:
                    runner_host_id = experiment_host(ctx.get("owner"), experiment_id)
                except Exception:
                    return {"error": "Experiment execution host is unavailable", "exit_code": 1}
            try:
                content_hash = await validate_environment(
                    ctx.get("owner"), runner_host_id, root,
                    scope=f"auto-research:{experiment_id}",
                )
            except Exception:
                return {"error": "Execution host could not freeze the environment", "exit_code": 1}
            return lab.configure_environment(
                ctx.get("owner"), experiment_id, split=split, root=root,
                manifest=manifest, validated_root=True,
                environment_content_hash=content_hash,
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
        if action == "start_lineage":
            result = lab.start_lineage(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                hypothesis=args.get("hypothesis") or "",
                trajectory_count=args.get("trajectory_count") or 3,
            )
            if result.get("exit_code") == 0 and args.get("auto_run", True):
                experiment_id = str(args.get("experiment_id") or "")
                prior = _research_drivers.get(experiment_id)
                if not prior or prior.done():
                    _research_drivers[experiment_id] = asyncio.create_task(
                        _drive_research(ctx.get("owner"), experiment_id, dict(ctx)),
                        name=f"auto-research-{experiment_id[:8]}",
                    )
                result["driver_status"] = "running"
            return result
        if action == "claim_work":
            return lab.claim_work(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                worker_id=str(args.get("worker_id") or (ctx.get("child_run_id") or "parent")),
                actor_role=str(args.get("actor_role") or ""),
                lease_seconds=args.get("lease_seconds") or 900,
                event_id=str(args.get("event_id") or ""),
            )
        if action == "submit_work":
            output = dict(args.get("output") or {})
            from src.auto_research_isolation import leased_stage
            try:
                stage = leased_stage(
                    ctx.get("owner"), str(args.get("experiment_id") or ""),
                    str(args.get("event_id") or ""), str(args.get("lease_token") or ""),
                )
            except Exception:
                return {"error": "Active stage lease not found", "exit_code": 1}
            if stage == "implementation":
                from src.auto_research_isolation import validate_implementation
                try:
                    output["_validated_candidate_sha"] = await validate_implementation(
                        ctx.get("owner"), str(args.get("experiment_id") or ""),
                        event_id=str(args.get("event_id") or ""),
                        lease_token=str(args.get("lease_token") or ""),
                        candidate_sha=str(output.get("candidate_sha") or ""),
                    )
                except Exception:
                    return {"error": "Execution host rejected the implementation commit", "exit_code": 1}
            return lab.submit_work(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                event_id=str(args.get("event_id") or ""),
                lease_token=str(args.get("lease_token") or ""),
                output=output, outcome=str(args.get("outcome") or "completed"),
            )
        if action == "workflow":
            return lab.workflow_snapshot(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                cursor=args.get("cursor") or 0,
            )
        if action == "run_workflow":
            experiment_id = str(args.get("experiment_id") or "")
            prior = _research_drivers.get(experiment_id)
            if not prior or prior.done():
                _research_drivers[experiment_id] = asyncio.create_task(
                    _drive_research(ctx.get("owner"), experiment_id, dict(ctx)),
                    name=f"auto-research-{experiment_id[:8]}",
                )
            return {"experiment_id": experiment_id, "driver_status": "running", "exit_code": 0}
        if action in {"pause", "resume", "close"}:
            return lab.set_status(ctx.get("owner"), str(args.get("experiment_id") or ""),
                                  {"pause": "paused", "resume": "open", "close": "closed"}[action])
        if action == "evaluate":
            return lab.evaluate(
                ctx.get("owner"), str(args.get("experiment_id") or ""),
                str(args.get("candidate_sha") or ""),
            )
        return {"error": "Unknown Auto-Research action", "exit_code": 1}
