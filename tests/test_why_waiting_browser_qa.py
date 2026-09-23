"""Opt-in real-browser QA for the content-free two-client stalled-run panel."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("ODYSSEUS_REAL_WAIT_BROWSER_QA") != "1",
    reason="explicit real-browser waiting-panel QA opt-in required",
)

SESSION_ID = "00000000-0000-4000-8000-000000000205"
ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_stalled_model_panel_matches_after_two_client_reload():
    from playwright.sync_api import sync_playwright

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="odysseus-wait-browser-qa-") as data_dir:
        log_file = (Path(data_dir) / "server.log").open("w")
        env = dict(os.environ)
        env.update({
            "AUTH_ENABLED": "false",
            "ODYSSEUS_ENABLE_REPLAY_QA": "1",
            "ODYSSEUS_WAIT_QA_STALLED": "1",
            "ODYSSEUS_DURABLE_CHAT_REPLAY": "1",
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
                pytest.fail("isolated waiting-panel fixture did not become healthy")

            with urlopen(base + "/api/chat/work/" + SESSION_ID + "/why-waiting", timeout=5) as response:
                stalled = json.load(response)
            assert stalled["phase"] == "model" and stalled["stalled"] is True
            assert stalled["recovery_action"] == "inspect"
            assert stalled["run_status"] == "running"
            assert stalled["checkpoint"]["durable_seq"] == 0
            assert len(stalled["checkpoint"]["ledger_hash"]) == 64
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    observed = []
                    for viewport in ({"width": 1280, "height": 800},
                                     {"width": 390, "height": 844}):
                        context = browser.new_context(viewport=viewport)
                        page = context.new_page()
                        errors = []
                        http_errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("console", lambda message: errors.append(message.text)
                                if message.type == "error" else None)
                        page.on("response", lambda response: http_errors.append((response.status, response.url))
                                if response.status >= 400 else None)
                        # This fixture has no research run. Supply its idle
                        # status so an unrelated expected 404 does not
                        # masquerade as waiting-panel console failures.
                        for pattern in ("**/api/research/status/*",):
                            page.route(pattern, lambda route: route.fulfill(
                                status=200, content_type="application/json",
                                body='{"status":"idle"}',
                            ))
                        page.goto(base + "/#" + SESSION_ID, wait_until="domcontentloaded")
                        assert "Odysseus" in page.title()
                        assert page.url.startswith(base + "/#" + SESSION_ID)
                        assert page.locator("#chat-container").is_visible()
                        assert not page.locator("text=/Next.js|Vite error|Webpack error/").count()
                        page.locator("#wait-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#wait-toggle").click()
                        assert page.locator("#wait-run-id").inner_text() == stalled["run_id"]
                        assert page.locator("#wait-model").inner_text() == "fixture-model"
                        assert page.locator("#wait-endpoint").inner_text() == "fixture-endpoint"
                        assert "seq 0" in page.locator("#wait-checkpoint").inner_text()
                        assert page.locator("#wait-action").is_visible()
                        observed.append(tuple(page.locator(selector).inner_text() for selector in (
                            "#wait-run-id", "#wait-model", "#wait-endpoint", "#wait-checkpoint",
                        )))
                        page.reload(wait_until="domcontentloaded")
                        page.locator("#wait-mode-status").wait_for(state="visible", timeout=15000)
                        page.locator("#wait-toggle").click()
                        assert page.locator("#wait-run-id").inner_text() == stalled["run_id"]
                        with urlopen(base + "/api/chat/work/" + SESSION_ID + "/why-waiting", timeout=5) as response:
                            after_reload = json.load(response)
                        assert after_reload["run_status"] == "running"
                        assert after_reload["phase"] == "model"
                        cancelled = page.locator("#chat-history .stopped-indicator").evaluate_all(
                            "nodes => nodes.map(node => ({text: node.textContent, "
                            "parent: node.parentElement?.parentElement?.outerHTML.slice(0, 500)}))"
                        )
                        with urlopen(base + "/api/history/" + SESSION_ID + "?limit=1", timeout=5) as response:
                            tail = json.load(response)
                        assert not any("[Cancelled by user]" in item["text"] for item in cancelled), (cancelled, tail)
                        assert not errors, (errors, http_errors)
                        assert "PRIVATE_CHILD_CONTEXT" not in page.locator("#wait-mode-status").inner_text()
                        screenshot = page.screenshot()
                        assert screenshot.startswith(b"\x89PNG")
                        screenshot_dir = os.getenv("ODYSSEUS_WAIT_BROWSER_SCREENSHOT_DIR")
                        if screenshot_dir:
                            output = Path(screenshot_dir)
                            output.mkdir(parents=True, exist_ok=True)
                            (output / f"wait-{viewport['width']}.png").write_bytes(screenshot)
                        context.close()
                    assert observed[0] == observed[1]
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)
            log_file.close()
