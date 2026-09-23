"""Opt-in real Chromium stress of the active-run replay window.

The app is isolated on loopback with synthetic data, no model and no host tool.
Set ODYSSEUS_REAL_REPLAY_BROWSER_QA=1; optionally set
ODYSSEUS_REPLAY_BROWSER_EVENTS=100000 for the full long-run corpus.
"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("ODYSSEUS_REAL_REPLAY_BROWSER_QA") != "1",
    reason="explicit real replay browser QA opt-in required",
)

SESSION_ID = "00000000-0000-4000-8000-000000000205"
ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _json(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def test_live_replay_keeps_browser_dom_bounded_and_pages_older_activity():
    from playwright.sync_api import sync_playwright

    event_count = min(100000, max(2000, int(os.getenv("ODYSSEUS_REPLAY_BROWSER_EVENTS", "10000"))))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="odysseus-real-replay-qa-") as data_dir:
        log_path = Path(data_dir) / "server.log"
        log_stream = log_path.open("w")
        env = dict(os.environ)
        env.update({
            "AUTH_ENABLED": "false",
            "ODYSSEUS_ENABLE_REPLAY_QA": "1",
            "ODYSSEUS_REPLAY_QA_EVENTS": str(event_count),
            "ODYSSEUS_REPLAY_QA_DELAY_MS": "1",
            "ODYSSEUS_DATA_DIR": data_dir,
            "DATABASE_URL": "sqlite:///" + str(Path(data_dir) / "app.db"),
            "PYTHONPATH": str(ROOT),
        })
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.fixtures.replay_browser_app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log_stream, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    pytest.fail(f"isolated replay server exited {server.returncode}")
                try:
                    if _json(base + "/api/health").get("status") == "healthy":
                        break
                except Exception:
                    time.sleep(0.2)
            else:
                pytest.fail("isolated replay server did not become healthy")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(base + "/#" + SESSION_ID, wait_until="domcontentloaded")
                    try:
                        page.locator("#chat-history .streaming").first.wait_for(timeout=60000)
                    except Exception as exc:
                        state = page.evaluate("""() => ({title: document.title,
                            historyChildren: document.querySelector('#chat-history')?.children.length,
                            bodyTail: document.body.innerText.slice(-300)})""")
                        run = _json(base + "/api/chat/run/" + SESSION_ID)
                        pytest.fail(f"initial replay unavailable: {exc}; page={state}; "
                                    f"run_status={run.get('status')}, last_seq={run.get('last_seq')}; "
                                    f"errors={errors}; server_log={log_path.read_text()[-3000:]}")
                    page.locator("#chat-context-pill").click()
                    assert page.locator(".chat-context-popup").get_by_text(
                        "Current serving window"
                    ).is_visible()
                    page.locator("#chat-context-pill").click()
                    if event_count >= 10000:
                        page.wait_for_function("""() => Array.from(document.querySelectorAll(
                            '#chat-history > .streaming[data-replay-seq]')).some(
                            node => Number(node.dataset.replaySeq) >= 500)""", timeout=30000)
                        bounds = page.locator("#chat-history").bounding_box()
                        assert bounds is not None
                        page.mouse.move(bounds["x"] + bounds["width"] / 2,
                                        bounds["y"] + bounds["height"] / 2)
                        page.mouse.wheel(0, -900)
                        first_reading_seq = page.locator(
                            "#chat-history > .streaming[data-replay-seq]"
                        ).first.get_attribute("data-replay-seq")
                        before_reading = int(_json(base + "/api/chat/run/" + SESSION_ID)["last_seq"])
                        if before_reading + 500 < event_count:
                            until = time.monotonic() + 10
                            while time.monotonic() < until and int(_json(
                                base + "/api/chat/run/" + SESSION_ID
                            )["last_seq"]) < before_reading + 500:
                                time.sleep(0.1)
                            assert page.locator(
                                "#chat-history > .streaming[data-replay-seq]"
                            ).first.get_attribute("data-replay-seq") == first_reading_seq
                        page.locator("#scroll-bottom-btn").click()
                        page.wait_for_function("""() => {
                            const box = document.querySelector('#chat-history');
                            return box && box.scrollHeight - box.scrollTop - box.clientHeight < 160;
                        }""", timeout=10000)
                    deadline = time.monotonic() + max(120, event_count / 300)
                    while time.monotonic() < deadline:
                        run = _json(base + "/api/chat/run/" + SESSION_ID)
                        last_seq = int(run.get("last_seq", -1))
                        dom = page.evaluate("""() => ({
                            count: document.querySelectorAll('#chat-history > .streaming').length,
                            lastSeq: Math.max(-1, ...Array.from(document.querySelectorAll('#chat-history > .streaming[data-replay-seq]'))
                              .slice(-4).map(node => Number(node.dataset.replaySeq))),
                            olderVisible: Array.from(document.querySelectorAll('#chat-history button'))
                              .some(button => button.textContent.includes('Load earlier run activity') && !button.hidden),
                          })""")
                        if last_seq >= event_count - 1 and dom["lastSeq"] >= event_count - 10:
                            break
                        time.sleep(0.25)
                    else:
                        pytest.fail(f"replay did not catch up: server={last_seq}, browser={dom}")
                    assert dom["count"] <= 360, dom
                    assert dom["olderVisible"], dom
                    assert not errors, errors
                    page.wait_for_function("""() => {
                        const box = document.querySelector('#chat-history');
                        return box && box.scrollHeight - box.scrollTop - box.clientHeight < 160;
                    }""", timeout=5000)
                    screenshot_path = os.getenv("ODYSSEUS_REPLAY_BROWSER_SCREENSHOT")
                    if screenshot_path:
                        page.screenshot(path=screenshot_path)
                    page.get_by_role("button", name="Load earlier run activity").click()
                    page.wait_for_function("""() => document.querySelectorAll('#chat-history [data-replay-preview]').length > 0""")
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#chat-history .streaming").first.wait_for(timeout=30000)
                    assert page.locator("#chat-history .streaming").count() <= 360
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            log_stream.close()
