"""Tests for the memory provider interface and native adapter."""

import asyncio


class FakeVectorStore:
    healthy = True

    def __init__(self):
        self.added = []
        self.removed = []
        self.results = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)

    def search(self, query, k=5):
        return self.results[:k]


def run(coro):
    return asyncio.run(coro)


def test_native_provider_remember_writes_native_memory_and_vector(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)

    record = run(provider.remember(
        "User prefers concise responses",
        owner="alice",
        session_id="session-1",
        category="preference",
        metadata={"confidence": 0.9},
    ))

    stored = manager.load(owner="alice")
    assert len(stored) == 1
    assert stored[0]["id"] == record.id
    assert stored[0]["text"] == "User prefers concise responses"
    assert stored[0]["category"] == "preference"
    assert stored[0]["session_id"] == "session-1"
    assert record.metadata["confidence"] == 0.9
    assert vector.added == [(record.id, "User prefers concise responses")]


def test_native_provider_recall_filters_vector_hits_by_owner(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)

    alice = run(provider.remember("Alice likes green tea", owner="alice"))
    bob = run(provider.remember("Bob likes espresso", owner="bob"))
    vector.results = [
        {"memory_id": bob.id, "score": 0.99},
        {"memory_id": alice.id, "score": 0.75},
    ]

    hits = run(provider.recall("what does Alice like?", owner="alice", top_k=5))

    assert [hit.memory.id for hit in hits] == [alice.id]
    assert hits[0].provider_id == "native"
    assert hits[0].score == 0.75


def test_native_provider_recall_filters_credential_memories_and_unowned_vector_rows(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)
    secret = run(provider.remember(
        "Jetson host sudo password: QA_SECRET_SENTINEL_NEVER_USE", owner="alice"))
    safe = run(provider.remember("Alice prefers concise technical answers", owner="alice"))
    vector.results = [
        {"memory_id": secret.id, "score": 1.0},
        {"memory_id": safe.id, "score": 0.8},
        {"text": "Access token: QA_VECTOR_SECRET_SENTINEL", "score": 1.0},
    ]

    hits = run(provider.recall("Jetson sudo password concise technical", owner="alice", top_k=5))

    assert [hit.memory.id for hit in hits] == [safe.id]
    assert "QA_SECRET_SENTINEL_NEVER_USE" not in repr(hits)
    assert "QA_VECTOR_SECRET_SENTINEL" not in repr(hits)


def test_native_provider_recall_drops_unowned_legacy_vector_rows_for_scoped_query(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)
    vector.results = [{"id": "legacy-1", "text": "private ownerless result", "timestamp": 5}]

    assert run(provider.recall("anything", owner="alice", top_k=5)) == []


def test_native_provider_list_omits_credential_rows_without_deleting_them(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    provider = NativeMemoryProvider(manager)
    secret = run(provider.remember(
        "Jetson host sudo password: QA_SECRET_SENTINEL_NEVER_USE", owner="alice"))
    safe = run(provider.remember("Alice prefers concise technical answers", owner="alice"))

    listed = run(provider.list_memories(owner="alice"))

    assert [memory.id for memory in listed] == [safe.id]
    assert "QA_SECRET_SENTINEL_NEVER_USE" not in repr(listed)
    assert any(entry["id"] == secret.id for entry in manager.load(owner="alice"))


def test_native_provider_recall_accepts_legacy_vector_rows(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)

    vector.results = [
        {"id": "legacy-1", "text": "real memory", "timestamp": 5},
        "corrupt-row",
        None,
    ]

    hits = run(provider.recall("anything", top_k=5))

    assert [hit.memory.id for hit in hits] == ["legacy-1"]
    assert hits[0].memory.text == "real memory"


def test_native_provider_recall_falls_back_to_keyword_search(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    provider = NativeMemoryProvider(manager)
    saved = run(provider.remember(
        "Alice prefers markdown notes",
        owner="alice",
        category="preference",
    ))

    hits = run(provider.recall("markdown preference", owner="alice", top_k=3))

    assert [hit.memory.id for hit in hits] == [saved.id]
    assert hits[0].score is None


def test_memory_provider_registry_exposes_only_active_provider_tools():
    from src.memory_provider import MemoryProvider, MemoryProviderRegistry

    class DummyProvider(MemoryProvider):
        def __init__(self, provider_id, enabled=True):
            self.provider_id = provider_id
            self.display_name = provider_id
            self.enabled = enabled

        async def remember(self, text, **kwargs):
            raise NotImplementedError

        async def recall(self, query, **kwargs):
            return []

        async def list_memories(self, **kwargs):
            return []

        async def delete(self, memory_id, **kwargs):
            return False

        def get_tool_schemas(self):
            return [{"name": f"{self.provider_id}_search", "description": "Search memory"}]

    registry = MemoryProviderRegistry([
        DummyProvider("active"),
        DummyProvider("disabled", enabled=False),
    ])

    assert registry.get_tool_schemas() == [
        {"name": "active_search", "description": "Search memory"}
    ]


def test_memory_provider_registry_rejects_tool_name_conflicts():
    from src.memory_provider import MemoryProvider, MemoryProviderRegistry

    class ConflictingProvider(MemoryProvider):
        def __init__(self, provider_id):
            self.provider_id = provider_id
            self.display_name = provider_id

        async def remember(self, text, **kwargs):
            raise NotImplementedError

        async def recall(self, query, **kwargs):
            return []

        async def list_memories(self, **kwargs):
            return []

        async def delete(self, memory_id, **kwargs):
            return False

        def get_tool_schemas(self):
            return [{"name": "memory_search"}]

    registry = MemoryProviderRegistry([
        ConflictingProvider("first"),
        ConflictingProvider("second"),
    ])

    try:
        registry.get_tool_schemas()
    except ValueError as exc:
        assert "memory_search" in str(exc)
    else:
        raise AssertionError("Expected duplicate memory tool names to be rejected")
