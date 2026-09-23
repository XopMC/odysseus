from src.memory_safety import is_sensitive_memory_text


def test_sensitive_memory_detector_catches_labelled_credentials_without_echoing():
    assert is_sensitive_memory_text("Jetson host sudo password: QA_SECRET_SENTINEL")
    assert is_sensitive_memory_text('api_key="QA_SECRET_SENTINEL"')
    assert is_sensitive_memory_text("Bearer QA_TOKEN_SENTINEL_0123456789")
    assert is_sensitive_memory_text("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_sensitive_memory_detector_keeps_ordinary_preferences_and_setup_notes():
    assert not is_sensitive_memory_text("User prefers concise technical answers.")
    assert not is_sensitive_memory_text("The project uses a password manager.")
    assert not is_sensitive_memory_text("We need to rotate the credentials after deployment.")


def test_model_memory_list_and_search_do_not_return_saved_credentials(tmp_path, monkeypatch):
    import asyncio

    from src import ai_interaction
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    unsafe = manager.add_entry(
        "Jetson host sudo password: QA_SECRET_SENTINEL_NEVER_USE", owner="alice")
    safe = manager.add_entry("Alice prefers concise technical answers", owner="alice")
    manager.save([unsafe, safe])
    monkeypatch.setattr(ai_interaction, "_memory_manager", manager)

    listed = asyncio.run(ai_interaction.do_manage_memory("list", owner="alice"))
    searched = asyncio.run(ai_interaction.do_manage_memory("search\nsudo password", owner="alice"))

    assert "QA_SECRET_SENTINEL_NEVER_USE" not in listed["results"]
    assert "QA_SECRET_SENTINEL_NEVER_USE" not in searched["results"]
    assert "concise technical answers" in listed["results"]
    # The filter does not delete or rewrite user-managed data.
    assert any(row["id"] == unsafe["id"] for row in manager.load(owner="alice"))


def test_model_memory_tool_refuses_to_store_credential_shaped_text(tmp_path, monkeypatch):
    import asyncio

    from src import ai_interaction
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    monkeypatch.setattr(ai_interaction, "_memory_manager", manager)

    result = asyncio.run(ai_interaction.do_manage_memory(
        "add\nSSH credential: QA_SECRET_SENTINEL_NEVER_USE", owner="alice"))

    assert "cannot be saved" in result["error"]
    assert manager.load(owner="alice") == []
