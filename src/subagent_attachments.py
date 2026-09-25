"""Explicit, owner-scoped attachment handoff for Agent children.

An upload ID is not authority by itself.  A child may receive only files that
were attached to a user turn of its exact parent chat, and the upload must
still belong to that owner when the child reads it.  No parent media bytes are
copied into the child row or its public timeline.
"""
from __future__ import annotations

import json
import os
from typing import Any

from src.attachment_refs import attachment_refs_from_metadata
from core.database import ChatMessage, Session, SessionLocal
from src.document_processor import build_user_content
from src.upload_handler import is_valid_upload_id


MAX_CHILD_ATTACHMENTS = 8
MAX_CHILD_ATTACHMENT_BYTES = 24 * 1024 * 1024


class ChildAttachmentError(ValueError):
    pass


def _attached_ids(owner: str | None, session_id: str) -> set[str]:
    db = SessionLocal()
    try:
        session = db.query(Session.id).filter(Session.id == session_id)
        session = session.filter(
            Session.owner == owner if owner else Session.owner.is_(None)
        ).one_or_none()
        if session is None:
            raise ChildAttachmentError("Parent chat is unavailable")
        attached: set[str] = set()
        rows = db.query(ChatMessage.meta_data).filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "user",
            ChatMessage.meta_data.isnot(None),
        ).yield_per(100)
        for (raw,) in rows:
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if isinstance(meta, dict):
                attached.update(
                    str(ref["attachment_id"])
                    for ref in attachment_refs_from_metadata(meta)
                    if ref.get("attachment_id")
                )
        return attached
    finally:
        db.close()


def authorize_child_attachments(
    owner: str | None, session_id: str, ids: Any, upload_handler: Any,
) -> tuple[list[str], dict[str, dict]]:
    """Return canonical IDs and live upload rows, or fail before child spawn."""
    if ids in (None, []):
        return [], {}
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CHILD_ATTACHMENTS:
        raise ChildAttachmentError("attachment_ids must contain 1–8 upload IDs")
    if any(not isinstance(value, str) or not is_valid_upload_id(value) for value in ids):
        raise ChildAttachmentError("attachment_ids contains an invalid upload ID")
    if len(set(ids)) != len(ids):
        raise ChildAttachmentError("attachment_ids contains duplicates")
    if upload_handler is None:
        raise ChildAttachmentError("Attachment service is unavailable")
    attached = _attached_ids(owner, session_id)
    if any(upload_id not in attached for upload_id in ids):
        raise ChildAttachmentError("A requested file is not attached to this chat")
    resolved: dict[str, dict] = {}
    total_bytes = 0
    for upload_id in ids:
        info = upload_handler.reserve_upload(
            upload_id, owner=owner, allow_admin=False,
        )
        if not isinstance(info, dict):
            raise ChildAttachmentError("A requested file is no longer available to this owner")
        path = info.get("path")
        if not isinstance(path, str) or not os.path.isfile(path):
            raise ChildAttachmentError("A requested file is unavailable")
        if hasattr(upload_handler, "_inside_upload_dir"):
            inside = upload_handler._inside_upload_dir(path)
        else:
            inside = upload_handler.inside_base_dir(path)
        if not inside:
            raise ChildAttachmentError("A requested file is outside the upload directory")
        total_bytes += os.path.getsize(path)
        if total_bytes > MAX_CHILD_ATTACHMENT_BYTES:
            raise ChildAttachmentError("Selected files exceed the child attachment budget")
        resolved[upload_id] = info
    return list(ids), resolved


def build_child_user_content(
    prompt: str, owner: str | None, session_id: str,
    ids: list[str], upload_handler: Any,
) -> str | list[dict]:
    """Recheck ownership when executing, then prepare bounded model input.

    Passing ``session_id=None`` to the regular attachment processor avoids
    its chat-UI side effect of creating PDF documents in the parent workspace.
    """
    canonical, resolved = authorize_child_attachments(
        owner, session_id, ids, upload_handler,
    )
    if not canonical:
        return prompt
    return build_user_content(
        prompt, canonical, upload_handler.upload_dir, upload_handler,
        session_id=None, owner=owner, resolved_uploads=resolved,
    )
