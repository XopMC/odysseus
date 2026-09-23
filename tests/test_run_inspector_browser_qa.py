"""Opt-in rendered inspector QA with a synthetic detached Agent run."""

import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("ODYSSEUS_REAL_INSPECTOR_BROWSER_QA") != "1",
    reason="explicit rendered run-inspector QA opt-in required",
)
ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "00000000-0000-4000-8000-000000000205"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_inspector_exact_events_two_clients_and_reload():
    from playwright.sync_api import sync_playwright

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="odysseus-inspector-browser-qa-") as data_dir:
        log_file = (Path(data_dir) / "server.log").open("w")
        env = dict(os.environ)
        env.update({
            "AUTH_ENABLED": "false", "ODYSSEUS_ENABLE_REPLAY_QA": "1",
            "ODYSSEUS_INSPECTOR_QA_SEED": "1",
            "ODYSSEUS_REPLAY_QA_EVENTS": "205", "ODYSSEUS_DURABLE_CHAT_REPLAY": "1",
            "ODYSSEUS_DATA_DIR": data_dir,
            "DATABASE_URL": "sqlite:///" + str(Path(data_dir) / "app.db"),
            "PYTHONPATH": str(ROOT),
        })
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.fixtures.replay_browser_app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    pytest.fail(f"isolated fixture exited {server.returncode}")
                try:
                    with urlopen(base + "/api/health", timeout=3) as response:
                        if json.load(response).get("status") == "healthy":
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                pytest.fail("isolated inspector fixture did not become healthy")
            while time.monotonic() < deadline:
                try:
                    with urlopen(base + "/api/chat/run/" + SESSION_ID, timeout=3) as response:
                        if json.load(response).get("last_seq", -1) >= 204:
                            break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("synthetic replay did not reach 205 events")
            with urlopen(base + "/api/chat/work/" + SESSION_ID + "/run-inspector", timeout=5) as response:
                inspector = json.load(response)
            assert inspector["runs"], inspector

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    identities = []
                    for viewport in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
                        context = browser.new_context(viewport=viewport)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("console", lambda message: errors.append(message.text)
                                if message.type == "error" else None)
                        page.route("**/api/research/status/*", lambda route: route.fulfill(
                            status=200, content_type="application/json", body='{"status":"idle"}'))
                        page.goto(base + "/#" + SESSION_ID, wait_until="domcontentloaded")
                        assert "Odysseus" in page.title()
                        assert page.locator("#chat-container").is_visible()
                        assert not page.locator("text=/Next.js|Vite error|Webpack error/").count()
                        page.locator("#wait-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#wait-toggle").click()
                        checkpoint_seq = int(re.search(r"seq (\d+)", page.locator("#wait-checkpoint").inner_text()).group(1))
                        page.locator("#wait-run-inspector").click()
                        dialog = page.locator("#run-inspector-dialog")
                        dialog.wait_for(state="visible", timeout=15000)
                        run_button = page.locator("#run-inspector-runs button[data-run-id]").first
                        run_button.wait_for(state="visible", timeout=15000)
                        run_id = run_button.get_attribute("data-run-id")
                        assert run_id and len(run_id) == 32
                        page.locator("#run-inspector-event-list button").first.wait_for(state="visible", timeout=15000)
                        assert f"#{checkpoint_seq}" in page.locator("#run-inspector-event-detail").inner_text()
                        assert page.locator("#run-inspector-event-list button").count() <= 200
                        assert page.locator("#run-inspector-older").is_visible()
                        page.locator("#run-inspector-older").click()
                        page.locator("#run-inspector-event-list button[data-event-seq='0']").wait_for(state="visible", timeout=15000)
                        first = page.locator("#run-inspector-event-list button").first
                        seq = first.get_attribute("data-event-seq")
                        first.click()
                        assert f"#{seq}" in page.locator("#run-inspector-event-detail").inner_text()
                        assert "[tool result fixture]" not in dialog.inner_text()
                        page.locator("#run-inspector-event-list button[data-event-kind='tool_output']").first.click()
                        page.locator("#run-inspector-load-tool-output").click()
                        page.locator("#run-inspector-artifact-body").get_by_text("[tool result fixture]").wait_for(state="visible", timeout=15000)
                        assert page.locator("#run-inspector-artifact-body").inner_text() == "[tool result fixture]"
                        page.locator("#run-inspector-close").click()
                        page.locator("#goal-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#goal-mode-status .chat-work-card-toggle").click()
                        page.locator("#goal-run-inspector").click()
                        page.locator("#run-inspector-runs button[data-run-id]").first.wait_for(state="visible", timeout=15000)
                        assert dialog.is_visible() and run_id in dialog.inner_text()
                        page.locator("#run-inspector-close").click()
                        page.locator("#plan-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#plan-mode-status .chat-work-card-toggle").click()
                        page.locator("#plan-run-inspector").click()
                        page.locator("#run-inspector-runs button[data-run-id]").first.wait_for(state="visible", timeout=15000)
                        assert dialog.is_visible() and run_id in dialog.inner_text()
                        page.locator("#run-inspector-close").click()
                        page.locator("#subagents-status").wait_for(state="visible", timeout=15000)
                        page.locator("#subagents-toggle").click()
                        page.locator("#subagents-list button[data-action='view']").first.click()
                        page.locator("#subagent-run-inspector").click()
                        page.locator("#run-inspector-runs .run-inspector-child.selected").wait_for(state="visible", timeout=15000)
                        assert page.locator("#run-inspector-runs .run-inspector-child.selected").get_attribute("data-child-id") == "fixture-child"
                        orphan = page.locator("#run-inspector-unlinked [data-child-id='orphan-child']")
                        orphan.wait_for(state="visible", timeout=15000)
                        assert "f" * 32 in orphan.inner_text()
                        page.locator("#run-inspector-runs button[data-artifact-id]").click()
                        page.locator("#run-inspector-artifact-body").get_by_text("SAFE synthetic evidence").wait_for(state="visible", timeout=15000)
                        assert page.locator("#run-inspector-artifact-body").inner_text() == "SAFE synthetic evidence"
                        identities.append(run_id)
                        page.reload(wait_until="domcontentloaded")
                        page.locator("#wait-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#wait-toggle").click()
                        page.locator("#wait-run-inspector").click()
                        page.locator("#run-inspector-runs button[data-run-id]").first.wait_for(state="visible", timeout=15000)
                        assert page.locator("#run-inspector-runs button[data-run-id]").first.get_attribute("data-run-id") == run_id
                        assert "[thinking fixture]" not in dialog.inner_text()
                        assert not errors, errors
                        screenshot_dir = os.getenv("ODYSSEUS_INSPECTOR_BROWSER_SCREENSHOT_DIR")
                        if screenshot_dir:
                            output = Path(screenshot_dir); output.mkdir(parents=True, exist_ok=True)
                            (output / f"inspector-{viewport['width']}.png").write_bytes(page.screenshot())
                        context.close()
                    assert identities[0] == identities[1]
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)
            log_file.close()
