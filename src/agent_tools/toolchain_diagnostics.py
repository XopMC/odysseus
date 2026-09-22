"""Fixed, bounded read-only environment inventory; never executes PATH shims."""

from __future__ import annotations

import asyncio
from importlib import metadata
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time


_PREFIXES = ("/usr/bin", "/bin", "/usr/local/bin", "/usr/local/Cellar",
             "/opt/homebrew/bin", "/opt/homebrew/Cellar", "/opt/local/bin",
             "/opt/local/libexec")
_TOOL_COMMANDS = {
    "node": ("/usr/bin/node", "/usr/local/bin/node", "/opt/homebrew/bin/node", "/opt/local/bin/node"),
    "git": ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git", "/opt/local/bin/git"),
    "clang": ("/usr/bin/clang", "/usr/local/bin/clang", "/opt/homebrew/bin/clang"),
    "gcc": ("/usr/bin/gcc", "/usr/local/bin/gcc", "/opt/homebrew/bin/gcc"),
    "docker": ("/usr/bin/docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker"),
    "podman": ("/usr/bin/podman", "/usr/local/bin/podman", "/opt/homebrew/bin/podman"),
    "pyright": ("/usr/bin/pyright", "/usr/local/bin/pyright", "/opt/homebrew/bin/pyright"),
    "pylsp": ("/usr/bin/pylsp", "/usr/local/bin/pylsp", "/opt/homebrew/bin/pylsp"),
    "typescript-language-server": ("/usr/bin/typescript-language-server",
                                   "/usr/local/bin/typescript-language-server",
                                   "/opt/homebrew/bin/typescript-language-server"),
}
_VERSION_ARGS = {"git": ("--version",), "node": ("--version",),
                 "clang": ("--version",), "gcc": ("--version",),
                 "docker": ("--version",), "podman": ("--version",),
                 "pyright": ("--version",), "pylsp": ("--version",),
                 "typescript-language-server": ("--version",)}
_PACKAGES = ("fastapi", "sqlalchemy", "uvicorn", "pytest", "playwright")
_ENDPOINT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _trusted_binary(candidate: str) -> str | None:
    try:
        resolved = os.path.realpath(candidate)
        if not any(os.path.commonpath((resolved, root)) == root for root in _PREFIXES):
            return None
        info = os.stat(resolved)
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _run_version(name: str) -> dict:
    for candidate in _TOOL_COMMANDS[name]:
        path = _trusted_binary(candidate)
        if not path:
            continue
        try:
            process = subprocess.Popen([path, *_VERSION_ARGS[name]], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError:
            continue
        output = bytearray()
        deadline = time.monotonic() + 2
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    chunk = os.read(process.stdout.fileno(), min(512, 1025 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > 1024:
                        raise ValueError("version output too large")
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if process.returncode:
                continue
            line = output.decode("utf-8", "replace").splitlines()[0] if output else ""
            line = "".join(char if 32 <= ord(char) < 127 else " " for char in line)[:240].strip()
            return {"status": "available", "version": line or "unknown"}
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
            if process.stdout:
                process.stdout.close()
    return {"status": "unavailable"}


def _package_versions() -> dict:
    found = {}
    for package in _PACKAGES:
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            found[package] = None
    return found


class InspectToolchainTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import host_execution
        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "Toolchain inspection is unavailable on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content or "{}")
            if (not isinstance(args, dict) or set(args) - {"endpoint_id"}
                    or ("endpoint_id" in args and (not isinstance(args["endpoint_id"], str)
                        or not _ENDPOINT_ID.fullmatch(args["endpoint_id"])))):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"error": "Invalid toolchain inspection arguments",
                    "code": "invalid_arguments", "exit_code": 1}
        tools = {"python": {"status": "available", "version": sys.version.split()[0]}}
        for name in _TOOL_COMMANDS:
            tools[name] = await asyncio.to_thread(_run_version, name)
        network = {"status": "not_checked"}
        if "endpoint_id" in args:
            from src.agent_tools.http_probe_tool import HttpProbeTool
            probe = await HttpProbeTool().execute(json.dumps({"endpoint_id": args["endpoint_id"]}), ctx)
            network = ({"status": probe.get("status"), "latency_ms": probe.get("total_ms"),
                        "method": "HEAD"} if probe.get("exit_code") == 0 else
                       {"status": "unavailable", "code": probe.get("code", "transport_unavailable")})
        packages = await asyncio.to_thread(_package_versions)
        output = "\n".join(f"{name}: {item.get('version', item['status'])}" for name, item in tools.items())
        return {"tools": tools, "packages": packages, "network": network,
                "output": output[:5000], "exit_code": 0}
