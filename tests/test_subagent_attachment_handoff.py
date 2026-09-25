import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatMessage, Session
from src import subagent_attachments as handoff


class Uploads:
    def __init__(self, root, rows):
        self.upload_dir = str(root)
        self.rows = rows

    def reserve_upload(self, upload_id, *, owner, allow_admin=False):
        info = self.rows.get(upload_id)
        return dict(info) if info and info.get("owner") == owner and not allow_admin else None

    @staticmethod
    def is_image_file(name, mime):
        return mime.startswith("image/")

    @staticmethod
    def is_audio_file(name, mime):
        return mime.startswith("audio/")

    @staticmethod
    def is_document_file(name, mime):
        return mime.startswith("text/")

    def _inside_upload_dir(self, path):
        return str(path).startswith(self.upload_dir + "/")


def _fixture(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(handoff, "SessionLocal", factory)
    attached = "a" * 32 + ".txt"
    other = "b" * 32 + ".txt"
    path = tmp_path / attached
    path.write_text("bounded delegated document", encoding="utf-8")
    other_path = tmp_path / other
    other_path.write_text("not attached", encoding="utf-8")
    db = factory()
    db.add(Session(id="chat", name="QA", endpoint_url="http://model", model="m", owner="alice"))
    db.add(ChatMessage(
        id="message", session_id="chat", role="user", content="See attachment",
        meta_data=json.dumps({"attachments": [{"id": attached, "name": "proof.txt", "mime": "text/plain"}]}),
    ))
    db.commit(); db.close()
    uploads = Uploads(tmp_path, {
        attached: {"id": attached, "name": "proof.txt", "mime": "text/plain", "path": str(path), "owner": "alice"},
        other: {"id": other, "name": "other.txt", "mime": "text/plain", "path": str(other_path), "owner": "alice"},
    })
    return attached, other, uploads


def test_child_gets_only_explicit_parent_attached_owner_file(monkeypatch, tmp_path):
    attached, other, uploads = _fixture(monkeypatch, tmp_path)
    ids, rows = handoff.authorize_child_attachments("alice", "chat", [attached], uploads)
    assert ids == [attached]
    assert set(rows) == {attached}
    child_content = handoff.build_child_user_content("Review this", "alice", "chat", ids, uploads)
    assert "bounded delegated document" in child_content
    assert "not attached" not in child_content

    with pytest.raises(handoff.ChildAttachmentError, match="not attached"):
        handoff.authorize_child_attachments("alice", "chat", [other], uploads)
    with pytest.raises(handoff.ChildAttachmentError, match="Parent chat"):
        handoff.authorize_child_attachments("mallory", "chat", [attached], uploads)
    with pytest.raises(handoff.ChildAttachmentError, match="duplicates"):
        handoff.authorize_child_attachments("alice", "chat", [attached, attached], uploads)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside upload directory", encoding="utf-8")
    uploads.rows[attached]["path"] = str(outside)
    with pytest.raises(handoff.ChildAttachmentError, match="outside the upload directory"):
        handoff.authorize_child_attachments("alice", "chat", [attached], uploads)


def test_empty_attachment_list_does_not_consult_upload_service():
    assert handoff.authorize_child_attachments("alice", "chat", [], None) == ([], {})


def test_schema_exposes_explicit_scoped_attachment_ids():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(item for item in FUNCTION_TOOL_SCHEMAS if item["function"]["name"] == "delegate_subagent")
    field = schema["function"]["parameters"]["properties"]["attachment_ids"]
    assert field["type"] == "array"
    assert field["maxItems"] == handoff.MAX_CHILD_ATTACHMENTS
