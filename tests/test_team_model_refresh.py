import json
from types import SimpleNamespace

from src import team_config


def test_refresh_models_is_owner_scoped_and_preserves_failed_cache(monkeypatch):
    owned = SimpleNamespace(
        id="owned", owner="alice", model_type="llm", base_url="http://local-a/v1",
        cached_models=json.dumps(["old-a"]), is_enabled=True,
    )
    failed = SimpleNamespace(
        id="failed", owner=None, model_type="llm", base_url="http://local-b/v1",
        cached_models=json.dumps(["old-b"]), is_enabled=True,
    )
    foreign = SimpleNamespace(
        id="foreign", owner="bob", model_type="llm", base_url="http://foreign/v1",
        cached_models=json.dumps(["private"]), is_enabled=True,
    )
    image = SimpleNamespace(
        id="image", owner="alice", model_type="image", base_url="http://image/v1",
        cached_models=json.dumps(["image-model"]), is_enabled=True,
    )

    class Query:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [owned, failed, foreign, image]

    class Db:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return Query()

        def commit(self):
            self.committed = True

    db = Db()
    monkeypatch.setattr("core.database.SessionLocal", lambda: db)
    def probe(url, key, timeout):
        assert key == "key" and timeout == 9
        if url == owned.base_url:
            return ["new-a", "new-a:2"]
        if url == failed.base_url:
            raise TimeoutError("offline")
        raise AssertionError("foreign or image endpoint was probed")

    result = team_config.refresh_models(
        "alice", timeout=9, probe=probe, key_resolver=lambda endpoint: "key",
    )

    assert result == {"refreshed_endpoints": 1, "failed_endpoint_ids": ["failed"]}
    assert json.loads(owned.cached_models) == ["new-a", "new-a:2"]
    assert json.loads(failed.cached_models) == ["old-b"]
    assert db.committed is True


def test_refresh_models_does_not_commit_when_every_probe_fails(monkeypatch):
    endpoint = SimpleNamespace(
        id="offline", owner="alice", model_type="llm", base_url="http://offline/v1",
        cached_models=json.dumps(["still-cached"]), is_enabled=True,
    )

    class Query:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [endpoint]

    class Db:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return Query()

        def commit(self):
            self.committed = True

    db = Db()
    monkeypatch.setattr("core.database.SessionLocal", lambda: db)
    result = team_config.refresh_models(
        "alice", probe=lambda *_args, **_kwargs: [], key_resolver=lambda endpoint: None,
    )

    assert result == {"refreshed_endpoints": 0, "failed_endpoint_ids": ["offline"]}
    assert json.loads(endpoint.cached_models) == ["still-cached"]
    assert db.committed is False
