"""Goal/Plan controls must remain discoverable without hiding the model route."""
from pathlib import Path
import os
import subprocess


def test_goal_and_plan_are_in_composer_overflow_and_model_picker_stays_visible():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'static/index.html').read_text()
    app = (root / 'static/app.js').read_text()
    chat = (root / 'static/js/chat.js').read_text()
    css = (root / 'static/style.css').read_text()
    assert 'id="plan-toggle-btn"' in html and 'id="goal-toggle-btn"' in html
    assert html.index('id="plan-toggle-btn"') < html.index('id="overflow-attach-btn"')
    assert "setGoalMode" in app and "goal_mode" in app
    assert "if (on) chatWork.prepareNewGoal?.();" in app
    assert "if (on) chatWork.prepareNewPlan?.();" in app
    assert "fd.append('goal_mode', choicesForSend.goal ? 'true' : 'false')" in chat
    assert "window.chatWork?.beginGoal?." in chat
    work = (root / "static" / "js" / "chat-work.js").read_text()
    renderer = (root / "static" / "js" / "chatRenderer.js").read_text()
    assert "beginGoal" in work
    assert "function prepareNewGoal()" in work
    assert "['completed', 'cancelled'].includes(snapshot.goal?.status)" in work
    assert "function prepareNewPlan()" in work
    assert "action === 'cancel') { snapshot.goal = null; window.__odysseusSetGoalMode?.(false); }" in work
    assert "goal?.status === 'cancelled'" in work
    assert "plan?.status === 'cancelled'" in work
    assert "bindUiText(state, 'Waiting for a goal')" in work
    assert "bindUiText(objective, 'Your next message becomes the active goal.')" in work
    assert "bindThinkingLabels(body)" in renderer
    assert "bindThinkingLabels(b)" in renderer
    sessions = (root / "static" / "js" / "sessions.js").read_text()
    assert "window.chatWork?.refresh?.(null);" in sessions
    routes = (root / "routes" / "chat_routes.py").read_text()
    goal_publish = 'yield f\'data: {json.dumps({"type": "goal_update", "data": active_goal})}'
    assert goal_publish in routes
    assert routes.index(goal_publish) < routes.index("async for chunk in stream_agent_loop(")
    stop_block = routes.split('async def chat_stop', 1)[1].split('return {"stopped": stopped, "goal": goal}', 1)[0]
    assert 'chat_work_store.goal_action(' in stop_block
    assert 'on_terminal=_goal_terminal_controller' in routes
    assert 'if active_goal:' in routes
    assert 'if active_goal and _user:' not in routes
    assert '"context_usage", "context_checkpoint", "compacted"' in routes
    assert 'f"{internal_api_base()}/api/chat_stream"' in routes
    assert 'nonlocal active_goal' in routes
    assert 'active_goal.get("status") == "waiting_user"' in routes
    assert 'chat_work_store.goal_action(\n                        owner, session, "resume"' in routes
    assert 'if _status == "error":' in routes
    assert "window.chatWork?.handleEvent?.({ type: 'goal_update', data: result.goal })" in chat
    assert "if (stopServer) {\n      window.chatWork?.pauseActiveGoal?.();" not in chat
    work_routes = (root / "routes" / "chat_work_routes.py").read_text()
    assert 'reason="goal_paused" if action == "pause" else "goal_cancelled"' in work_routes
    assert 'reason="plan_cancelled"' in work_routes
    assert "window.refreshChatContextHeader?.('goal-paused')" in work
    assert "additional guidance for the active goal" in chat
    assert "await window.chatWork.addGuidance(goalGuidance)" in chat
    assert "/goal-guidance" in work
    assert "appendGoalGuidance" in chat and "appendGoalGuidance" in work
    assert "setTimeout(continueGoal, 350)" not in work
    assert "if (active) setPlanMode" not in app
    assert 'id="chat-work-status-row"' in html
    assert "modelPickerWrap.classList.remove('model-picker-autohide')" in app
    assert "pickerWrap.classList.remove('picker-auto-hidden')" in app
    assert 'body.plan-mode-active .chat-input-top > .model-picker-wrap { opacity: 1' in css
    assert '#model-picker-wrap { display: none !important; }' not in css
    assert '.chat-input-top > .model-picker-wrap.picker-auto-hidden {\n      opacity: 1;' in css


def test_goal_card_and_model_route_do_not_overlap_on_desktop_or_phone():
    root = Path(__file__).resolve().parents[1]
    if subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        return
    script = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
try { for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
 const page=await browser.newPage({viewport});
 await page.setContent(`<main style="padding:8px"><div id="chat-work-status-row" class="chat-work-status-row"><section class="chat-work-card"><div class="chat-work-card-head"><strong>Цель</strong><span>активна · попытка 2</span></div><div class="chat-work-current">Проверить релиз с длинным текстом цели</div></section></div><div class="chat-input-bar"><div class="chat-input-top"><textarea id="message">текст</textarea><div id="model-picker-wrap" class="model-picker-wrap picker-auto-hidden"><button class="model-picker-btn"><span id="model-picker-label">qwen3.8-27b</span></button><div class="model-picker-list"><div class="model-switch-item"><span class="mp-model-name">qwen</span><span class="model-switch-ep">http://192.168.50.6:11434/v1</span></div></div></div></div></div></main>`);
 await page.addStyleTag({path:process.argv[1]+'/static/style.css'});
 const boxes=await page.evaluate(()=>{const a=document.querySelector('.chat-work-card').getBoundingClientRect(),b=document.querySelector('.chat-input-bar').getBoundingClientRect(),m=document.getElementById('model-picker-wrap').getBoundingClientRect(),ep=document.querySelector('.model-switch-ep');return {a:{top:a.top,bottom:a.bottom,left:a.left,right:a.right,width:a.width},b:{top:b.top,bottom:b.bottom,left:b.left,right:b.right,width:b.width},m:{width:m.width,height:m.height,display:getComputedStyle(document.getElementById('model-picker-wrap')).display,opacity:getComputedStyle(document.getElementById('model-picker-wrap')).opacity},ep:{overflow:getComputedStyle(ep).overflow,textOverflow:getComputedStyle(ep).textOverflow,whiteSpace:getComputedStyle(ep).whiteSpace}}});
 assert(boxes.a.bottom<=boxes.b.top,'goal card overlaps composer');assert.notEqual(boxes.m.display,'none');assert.equal(boxes.m.opacity,'1');assert(boxes.m.width>0&&boxes.m.height>0);assert.equal(boxes.ep.textOverflow,'clip');assert.equal(boxes.ep.whiteSpace,'normal');
 assert(Math.abs(boxes.a.width-boxes.b.width)<1,'goal width must equal composer width');
 assert(Math.abs(boxes.a.left-boxes.b.left)<1&&Math.abs(boxes.a.right-boxes.b.right)<1,'goal edges must align with composer');
 }} finally {await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script, str(root)], capture_output=True, text=True,
                            timeout=45, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
