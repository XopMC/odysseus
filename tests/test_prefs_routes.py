import json

import routes.prefs_routes as prefs_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_load_ignores_non_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps(["not", "a", "prefs", "object"]), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load() == {}
    assert prefs_routes._load_for_user("alice") == {}


def test_load_keeps_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load_for_user(None) == {"theme": "dark"}
    assert prefs_routes._load_for_user("alice") == {}


def test_named_preference_write_does_not_copy_flat_fallback_consent(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({
        "theme": "light",
        "foreground_fallback_enabled": True,
        "foreground_model_fallbacks": [
            {"endpoint_id": "legacy-single-user", "model": "legacy-model"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    bob = prefs_routes._load_for_user("bob")
    bob["theme"] = "dark"
    prefs_routes._save_for_user("bob", bob)

    raw = prefs_routes._load()
    assert raw["_users"] == {"bob": {"theme": "dark"}}
    assert raw["foreground_fallback_enabled"] is True
    assert raw["foreground_model_fallbacks"][0]["endpoint_id"] == "legacy-single-user"


def test_model_favorites_are_owner_scoped_revisioned_routes(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(prefs_routes, "get_current_user", lambda request: request.headers.get("x-user"))
    app = FastAPI()
    app.include_router(prefs_routes.setup_prefs_routes())
    client = TestClient(app)
    key = "endpoint-a::same-model"
    assert client.post("/api/prefs/model-favorites/toggle", headers={"x-user": "alice"}, json={"key": key, "favorite": True, "expected_revision": 0}).status_code == 200
    assert client.get("/api/prefs/model-favorites/snapshot", headers={"x-user": "alice"}).json()["favorites"] == [key]
    assert client.get("/api/prefs/model-favorites/snapshot", headers={"x-user": "bob"}).json()["favorites"] == []
    assert client.post("/api/prefs/model-favorites/toggle", headers={"x-user": "alice"}, json={"key": key, "favorite": False, "expected_revision": 0}).status_code == 409
