"""Bounded, explicitly profiled project verification (never a free-form shell)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path


_OUTPUT_LIMIT = 12000
_ARTIFACT_LIMIT = 1024 * 1024
_MAX_SECONDS = 300


def discover_profiles(root: str) -> dict[str, list[str]]:
    directory = Path(root)
    profiles: dict[str, list[str]] = {}
    if any((directory / name).is_file() for name in ("pytest.ini", "pyproject.toml", "setup.cfg")) or (directory / "tests").is_dir():
        project_python = directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = str(project_python) if project_python.is_file() and os.access(project_python, os.X_OK) else sys.executable
        profiles["pytest"] = [python, "-m", "pytest", "-q"]
    package = directory / "package.json"
    if package.is_file() and not package.is_symlink() and package.stat().st_size <= 262144:
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (UnicodeError, ValueError, OSError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for kind in ("test", "lint"):
                if isinstance(scripts.get(kind), str) and scripts[kind].strip():
                    profiles[f"npm_{kind}"] = ["npm", "run", kind]
    return profiles


async def run_profile(root: str, profile: str, timeout_seconds: int) -> dict:
    profiles = discover_profiles(root)
    if profile not in profiles:
        return {"error": "Verification profile is unavailable", "code": "not_found",
                "available_profiles": sorted(profiles), "exit_code": 1}
    command = profiles[profile]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=root, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "CI": "1", "NO_COLOR": "1"},
            start_new_session=True,
        )
    except OSError:
        return {"error": "Verification runtime is unavailable", "code": "transport_unavailable", "exit_code": 1}
    timed_out = False
    output = bytearray()
    truncated = False
    try:
        async def collect() -> None:
            nonlocal truncated
            assert process.stdout is not None
            while chunk := await process.stdout.read(8192):
                if len(output) < _ARTIFACT_LIMIT:
                    output.extend(chunk[:_ARTIFACT_LIMIT - len(output)])
                if len(output) >= _ARTIFACT_LIMIT:
                    truncated = True
        await asyncio.wait_for(asyncio.gather(collect(), process.wait()), timeout_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        timed_out = isinstance(exc, asyncio.TimeoutError)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()
        if not timed_out:
            raise
    text = output.decode("utf-8", "replace")
    return {"profile": profile, "command": command, "output": text[:_OUTPUT_LIMIT],
            "full_output": text,
            "truncated": truncated or len(text) > _OUTPUT_LIMIT, "timed_out": timed_out,
            "exit_code": 124 if timed_out else process.returncode,
            "code": "timeout" if timed_out else ("failed" if process.returncode else "ok")}


class RunVerificationTool:
    def __init__(self, kind: str):
        self.kind = kind

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_search_root
        from src import host_execution

        if host_execution.enabled_for(ctx.get("owner")):
            return await host_execution.execute(f"run_{self.kind}s" if self.kind == "test" else "run_lint", content,
                                                owner=ctx.get("owner"), session_id=ctx.get("session_id"),
                                                run_id=ctx.get("parent_run_id"))
        try:
            args = json.loads(content or "{}")
            if (not isinstance(args, dict) or set(args) - {"path", "profile", "timeout_seconds"}
                    or not isinstance(args.get("path", ""), str)
                    or not isinstance(args.get("profile", ""), str)
                    or type(args.get("timeout_seconds", 120)) is not int
                    or not 1 <= args.get("timeout_seconds", 120) <= _MAX_SECONDS):
                raise ValueError
            root = _resolve_search_root(args.get("path", ""))
            if not os.path.isdir(root):
                raise ValueError
            available = discover_profiles(root)
            allowed = {"pytest", "npm_test"} if self.kind == "test" else {"npm_lint"}
            profile = args.get("profile") or (
                ("pytest" if "pytest" in available else "npm_test")
                if self.kind == "test" else "npm_lint"
            )
            if profile == "list":
                return {"available_profiles": sorted(set(available) & allowed),
                        "code": "ok", "exit_code": 0}
            if profile not in allowed:
                raise ValueError
            if args.get("profile") is None and profile not in available:
                return {"available_profiles": sorted(set(available) & allowed),
                        "error": "No default verification profile is available",
                        "code": "not_found", "exit_code": 1}
            result = await run_profile(root, profile, args.get("timeout_seconds", 120))
            full_output = result.pop("full_output", "")
            if result.get("exit_code") != 0 or result.get("truncated"):
                try:
                    from src.observation_pack import archive
                    artifact = archive(ctx.get("owner"), ctx.get("session_id"),
                                       tool_name=f"run_{self.kind}", tool_call_id=str(ctx.get("tool_call_id") or "verification"),
                                       text=full_output, force=True,
                                       run_id=ctx.get("parent_run_id"))
                    if artifact:
                        result["artifact"] = artifact
                except (OSError, ValueError):
                    result["artifact_error"] = "Verification output could not be archived"
            return result
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return {"error": "Invalid verification arguments or path", "code": "invalid_arguments", "exit_code": 1}
