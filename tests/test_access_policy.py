from src.access_policy import access_mode_requires_approval, normalize_access_mode
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action


def test_access_mode_normalization_and_effect_classification():
    assert normalize_access_mode("full") == "full_access"
    assert normalize_access_mode("garbage") == "ask_important"
    assert access_mode_requires_approval(
        "ask_important", capabilities_for_action("bash", "echo ok")
    ) is True
    assert access_mode_requires_approval(
        "ask_important", capabilities_for_action("read_file", "README.md")
    ) is False
    assert access_mode_requires_approval(
        "ask_every_time", capabilities_for_action("read_file", "README.md")
    ) is True
    assert access_mode_requires_approval(
        "full_access", capabilities_for_action("bash", "echo ok")
    ) is False


def test_full_access_suppresses_all_interactive_approval_prompts():
    context = ToolRunSecurityContext(access_mode="full_access")
    assert context.decision_for("bash", "echo ok").allowed is True
    tainted = ToolRunSecurityContext(
        access_mode="full_access", external_untrusted_context_seen=True
    )
    assert tainted.decision_for("bash", "echo ok").allowed is True


def test_ask_every_time_does_not_inherit_task_bypass():
    context = ToolRunSecurityContext(
        access_mode="ask_every_time", approval_gate_bypassed=True
    )
    decision = context.decision_for("bash", "echo ok")
    assert decision.allowed is False
    assert "approval" in (decision.reason or "").lower()
