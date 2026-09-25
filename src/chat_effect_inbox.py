"""Owner-scoped Agent effect ledger; unknown actions never auto-replay."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from core.database import ChatToolIntent, ChatWorkEvent, SessionLocal, reserve_sqlite_writer, utcnow_naive
from src.chat_work_store import WorkConflict, WorkNotFound, _session, _storage_owner
from src.tool_capabilities import ToolEffect, capabilities_for_action


_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.:]{0,99}\Z")
_EFFECTFUL = frozenset({
    ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_PRIVATE,
    ToolEffect.EXECUTE_CODE, ToolEffect.NETWORK_EGRESS,
    ToolEffect.EXTERNAL_SIDE_EFFECT, ToolEffect.UI_SIDE_EFFECT,
    ToolEffect.ADMIN_CHANGE, ToolEffect.DESTRUCTIVE,
})


def needs_effect_intent(tool_name, content):
    capabilities = capabilities_for_action(tool_name, content)
    return not capabilities.known or bool(capabilities.effects & _EFFECTFUL)


def _public(row, *, created=False):
    receipt = dict(row.receipt or {})
    verification = receipt.get("verification")
    retry_authorization_id = receipt.get("retry_authorization_id")
    return {
        "id": row.id, "session_id": row.session_id, "run_id": row.run_id,
        "tool_call_id": row.tool_call_id, "tool_name": row.tool_name,
        "action_hash": row.action_hash, "status": row.status,
        "revision": row.revision, "receipt_hash": row.receipt_hash,
        "verification_outcome": (
            verification.get("outcome") if isinstance(verification, dict) else None
        ),
        "retry_authorization_id": (
            retry_authorization_id if isinstance(retry_authorization_id, str) else None
        ),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created": created,
    }


class ChatEffectInbox:
    @staticmethod
    def _event(db, row, kind):
        db.add(ChatWorkEvent(
            session_id=row.session_id, owner=row.owner,
            kind=kind, entity_id=row.id, revision=row.revision,
            payload={"intent_id": row.id, "status": row.status},
        ))

    @staticmethod
    def _goal_cutoff(goal_created_at):
        if goal_created_at is None:
            return None
        if not isinstance(goal_created_at, str):
            raise ValueError("Goal creation time required")
        cutoff = datetime.fromisoformat(goal_created_at)
        return cutoff.astimezone(timezone.utc).replace(tzinfo=None) if cutoff.tzinfo else cutoff

    @staticmethod
    def _no_retry_row(db, owner, session_id, tool_name, digest, cutoff):
        if cutoff is None:
            return None
        return db.query(ChatToolIntent).filter(
            ChatToolIntent.owner == _storage_owner(owner),
            ChatToolIntent.session_id == session_id,
            ChatToolIntent.tool_name == tool_name,
            ChatToolIntent.action_hash == digest,
            ChatToolIntent.status == "no_retry",
            ChatToolIntent.created_at >= cutoff,
        ).first()

    def no_retry_match(self, owner, session_id, tool_name, content, *, goal_created_at=None):
        """Return a same-Goal no-retry fence without exposing tool arguments."""
        cutoff = self._goal_cutoff(goal_created_at)
        if cutoff is None:
            return None
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with SessionLocal() as db:
            _session(db, owner, session_id)
            row = self._no_retry_row(db, owner, session_id, tool_name, digest, cutoff)
            return _public(row) if row is not None else None

    def record_intent(self, owner, session_id, run_id, tool_call_id, tool_name, content,
                      *, goal_created_at=None):
        if not isinstance(run_id, str) or not _HEX32.fullmatch(run_id):
            raise ValueError("Exact run ID required")
        if not isinstance(tool_call_id, str) or not tool_call_id or len(tool_call_id) > 200 or "\n" in tool_call_id:
            raise ValueError("Exact tool call ID required")
        if not isinstance(tool_name, str) or not _TOOL.fullmatch(tool_name):
            raise ValueError("Valid tool name required")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Bounded tool arguments required")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        cutoff = self._goal_cutoff(goal_created_at)
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            _session(db, owner, session_id)
            row = db.query(ChatToolIntent).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
                run_id=run_id, tool_call_id=tool_call_id,
            ).first()
            if row is not None:
                if row.tool_name != tool_name or row.action_hash != digest:
                    raise WorkConflict("Tool call ID was reused for a different action")
                return _public(row)
            if self._no_retry_row(db, owner, session_id, tool_name, digest, cutoff) is not None:
                raise WorkConflict("Exact action was marked Do not retry for this Goal")
            verified_not_applied = db.query(ChatToolIntent).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
                tool_name=tool_name, action_hash=digest, status="verified_not_applied",
            ).first()
            retry_authorization = db.query(ChatToolIntent).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
                tool_name=tool_name, action_hash=digest, status="retry_authorized",
            ).order_by(ChatToolIntent.created_at.asc(), ChatToolIntent.id.asc()).with_for_update().first()
            if verified_not_applied is not None and retry_authorization is None:
                raise WorkConflict(
                    "Matching action was verified not applied; explicit retry authorization required"
                )
            retry_link = None
            if retry_authorization is not None:
                old_receipt_hash = retry_authorization.receipt_hash
                consumed = {
                    "kind": "matching_retry_consumed",
                    "intent_id": retry_authorization.id,
                    "action_hash": digest,
                    "new_run_id": run_id,
                    "new_tool_call_id": tool_call_id,
                    "previous_revision": retry_authorization.revision,
                    "recorded_at": utcnow_naive().isoformat(),
                }
                retry_authorization.status = "retry_consumed"
                retry_authorization.revision += 1
                retry_authorization.receipt = {
                    **dict(retry_authorization.receipt or {}), "retry_consumed": consumed,
                }
                retry_authorization.receipt_hash = hashlib.sha256(json.dumps(
                    retry_authorization.receipt, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                self._event(db, retry_authorization, "effect_reconciled")
                retry_link = {
                    "retry_authorization_id": retry_authorization.id,
                    "retry_authorization_hash": old_receipt_hash,
                }
            row = ChatToolIntent(
                id=uuid.uuid4().hex, owner=_storage_owner(owner), session_id=session_id,
                run_id=run_id, tool_call_id=tool_call_id, tool_name=tool_name,
                action_hash=digest, status="intent", revision=1,
                receipt=retry_link,
            )
            db.add(row)
            db.flush()
            return _public(row, created=True)

    def _row(self, db, owner, session_id, intent_id):
        _session(db, owner, session_id)
        row = db.query(ChatToolIntent).filter_by(
            id=intent_id, owner=_storage_owner(owner), session_id=session_id,
        ).first()
        if row is None:
            raise WorkNotFound("Tool intent not found")
        return row

    def mark_unknown(self, owner, session_id, intent_id):
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            row = self._row(db, owner, session_id, intent_id)
            if row.status == "unknown":
                return _public(row)
            if row.status != "intent":
                raise WorkConflict("Only an open intent can become unknown")
            row.status = "unknown"
            row.revision += 1
            self._event(db, row, "effect_unknown")
            db.flush()
            return _public(row)

    def record_result(self, owner, session_id, intent_id, result):
        if not isinstance(result, dict):
            raise ValueError("Tool result must be an object")
        digest = hashlib.sha256(json.dumps(
            result, sort_keys=True, ensure_ascii=False, default=str,
        ).encode("utf-8")).hexdigest()
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            row = self._row(db, owner, session_id, intent_id)
            if row.status != "intent":
                raise WorkConflict("Tool result is already settled or uncertain")
            unknown = result.get("outcome_unknown") is True
            row.status = "unknown" if unknown else "done"
            row.receipt_hash = digest
            row.receipt = {"result_sha256": digest, "outcome_unknown": unknown}
            row.revision += 1
            if unknown:
                self._event(db, row, "effect_unknown")
            db.flush()
            return _public(row)

    def verify(self, owner, session_id, intent_id, *, expected_revision, outcome, evidence):
        """Record a content-free user verification receipt for an unknown effect."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Exact effect revision required")
        if outcome not in {"applied", "not_applied"}:
            raise ValueError("Verification outcome must be applied or not_applied")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence.encode("utf-8")) > 4096 or "\0" in evidence:
            raise ValueError("Bounded verification evidence is required")
        evidence_hash = hashlib.sha256(evidence.strip().encode("utf-8")).hexdigest()
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            row = self._row(db, owner, session_id, intent_id)
            if row.status != "unknown" or row.revision != expected_revision:
                raise WorkConflict("Tool intent changed; reload before verifying")
            verification = {
                "kind": "user_verified_effect",
                "outcome": outcome,
                "intent_id": row.id,
                "action_hash": row.action_hash,
                "evidence_sha256": evidence_hash,
                "evidence_bytes": len(evidence.strip().encode("utf-8")),
                "previous_revision": row.revision,
                "recorded_at": utcnow_naive().isoformat(),
            }
            row.status = "verified" if outcome == "applied" else "verified_not_applied"
            row.revision += 1
            row.receipt = {**dict(row.receipt or {}), "verification": verification}
            row.receipt_hash = hashlib.sha256(json.dumps(
                row.receipt, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            self._event(db, row, "effect_reconciled")
            db.flush()
            return _public(row)

    def authorize_retry(self, owner, session_id, intent_id, *, expected_revision):
        """Authorize one future, exact-hash retry; never replays the old payload."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Exact effect revision required")
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            row = self._row(db, owner, session_id, intent_id)
            if row.status != "verified_not_applied" or row.revision != expected_revision:
                raise WorkConflict("Only the current verified-not-applied intent can authorize retry")
            verification = dict(row.receipt or {}).get("verification")
            if not isinstance(verification, dict) or verification.get("outcome") != "not_applied":
                raise WorkConflict("A valid not-applied verification receipt is required")
            authorization = {
                "kind": "user_authorized_one_shot_retry",
                "intent_id": row.id,
                "action_hash": row.action_hash,
                "verification_receipt_hash": row.receipt_hash,
                "previous_revision": row.revision,
                "recorded_at": utcnow_naive().isoformat(),
            }
            row.status = "retry_authorized"
            row.revision += 1
            row.receipt = {**dict(row.receipt or {}), "retry_authorization": authorization}
            row.receipt_hash = hashlib.sha256(json.dumps(
                row.receipt, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            self._event(db, row, "effect_reconciled")
            db.flush()
            return _public(row)

    def mark_interrupted_run_unknown(self, owner, session_id, run_id):
        if not isinstance(run_id, str) or not _HEX32.fullmatch(run_id):
            raise ValueError("Exact run ID required")
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            _session(db, owner, session_id)
            rows = db.query(ChatToolIntent).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
                run_id=run_id, status="intent",
            ).all()
            for row in rows:
                row.status = "unknown"
                row.revision += 1
                self._event(db, row, "effect_unknown")
            return len(rows)

    def unresolved(self, owner, session_id):
        with SessionLocal() as db:
            _session(db, owner, session_id)
            rows = db.query(ChatToolIntent).filter(
                ChatToolIntent.owner == _storage_owner(owner),
                ChatToolIntent.session_id == session_id,
                ChatToolIntent.status.in_(("intent", "unknown")),
            ).order_by(ChatToolIntent.created_at, ChatToolIntent.id).limit(200).all()
            return [_public(row) for row in rows]

    def unknown(self, owner, session_id):
        """Only uncertain effects fence dispatch, even behind a long intent tail."""
        with SessionLocal() as db:
            _session(db, owner, session_id)
            rows = db.query(ChatToolIntent).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
                status="unknown",
            ).order_by(ChatToolIntent.created_at, ChatToolIntent.id).limit(200).all()
            return [_public(row) for row in rows]

    def blocking(self, owner, session_id):
        """Effects that still require a human decision before a Goal resumes."""
        with SessionLocal() as db:
            _session(db, owner, session_id)
            rows = db.query(ChatToolIntent).filter(
                ChatToolIntent.owner == _storage_owner(owner),
                ChatToolIntent.session_id == session_id,
                ChatToolIntent.status.in_(("unknown", "verified_not_applied")),
            ).order_by(ChatToolIntent.created_at, ChatToolIntent.id).limit(200).all()
            return [_public(row) for row in rows]

    def pending_actions(self, owner, session_id):
        """Owner inbox for unresolved effects and one-shot retry decisions."""
        with SessionLocal() as db:
            _session(db, owner, session_id)
            rows = db.query(ChatToolIntent).filter(
                ChatToolIntent.owner == _storage_owner(owner),
                ChatToolIntent.session_id == session_id,
                ChatToolIntent.status.in_(("unknown", "verified_not_applied", "retry_authorized")),
            ).order_by(ChatToolIntent.created_at, ChatToolIntent.id).limit(200).all()
            return [_public(row) for row in rows]

    def no_retry(self, owner, session_id, intent_id, *, expected_revision):
        """Explicitly retire uncertainty without claiming that the effect was verified.

        The server issues a digest-linked *decision* receipt, not a proof of
        what the external tool did. No action body or replay permission is kept.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Exact revision required")
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            row = self._row(db, owner, session_id, intent_id)
            if row.status not in {"unknown", "verified_not_applied", "retry_authorized"} or row.revision != expected_revision:
                raise WorkConflict("Tool intent changed; reload before reconciling")
            receipt = {
                "kind": "user_no_retry", "intent_id": row.id,
                "action_hash": row.action_hash, "owner": row.owner,
                "previous_status": row.status,
                "previous_revision": row.revision,
                "recorded_at": utcnow_naive().isoformat(),
            }
            row.status = "no_retry"
            row.receipt_hash = hashlib.sha256(json.dumps(
                receipt, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            row.receipt = {**dict(row.receipt or {}), "decision": receipt}
            row.revision += 1
            self._event(db, row, "effect_reconciled")
            db.flush()
            return _public(row)


inbox = ChatEffectInbox()
