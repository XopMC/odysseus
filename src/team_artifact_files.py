"""Bounded, owner-scoped binary artifacts for Team browser evidence."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path

from src.constants import DATA_DIR

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
_TYPES = {
    'image/png': ('.png', b'\x89PNG\r\n\x1a\n'),
    'image/jpeg': ('.jpg', b'\xff\xd8\xff'),
}


def _part(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('Invalid artifact identity')
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _root(owner: str, team_id: str) -> Path:
    base = (Path(DATA_DIR) / 'team-artifacts').resolve()
    root = (base / _part(owner) / _part(team_id)).resolve()
    if base not in root.parents:
        raise ValueError('Invalid artifact storage path')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _decode(image: object) -> tuple[bytes, str, str]:
    if not isinstance(image, dict):
        raise ValueError('Browser screenshot must be an object')
    mime, raw = image.get('mimeType'), image.get('data')
    if mime not in _TYPES or not isinstance(raw, str) or not raw or len(raw) > MAX_IMAGE_BYTES * 2:
        raise ValueError('Unsupported or oversized browser screenshot')
    try:
        value = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Browser screenshot is not valid base64') from exc
    suffix, magic = _TYPES[mime]
    if not value.startswith(magic) or len(value) > MAX_IMAGE_BYTES:
        raise ValueError('Browser screenshot type or size is invalid')
    return value, mime, suffix


def persist_screenshots(store, owner: str, team_id: str, worker_id: str, lease_token: str,
                        images: object) -> list[dict]:
    """Persist bounded screenshots; no model/MCP path controls filenames."""
    if not isinstance(images, list) or not 0 < len(images) <= MAX_IMAGES:
        raise ValueError('Browser screenshot count is invalid')
    decoded = [_decode(image) for image in images]
    if sum(len(value) for value, _, _ in decoded) > MAX_TOTAL_BYTES:
        raise ValueError('Browser screenshots exceed total size limit')
    root, result = _root(owner, team_id), []
    for index, (value, mime, suffix) in enumerate(decoded, start=1):
        temporary = root / ('.pending-' + os.urandom(16).hex())
        try:
            with open(temporary, 'xb') as handle:
                os.chmod(temporary, 0o600)
                handle.write(value); handle.flush(); os.fsync(handle.fileno())
            artifact = store.add_artifact(owner, team_id, 'Browser screenshot ' + str(index), {
                'kind': 'browser_screenshot', 'content_type': mime, 'bytes': len(value),
                'untrusted_content': True,
            }, worker_id=worker_id, lease_token=lease_token)
            os.replace(temporary, root / (artifact['id'] + suffix))
            result.append({'id': artifact['id'], 'content_type': mime, 'bytes': len(value)})
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return result


def open_screenshot(store, owner: str, team_id: str, artifact_id: str) -> tuple[Path, str]:
    artifact = store.get_artifact(owner, team_id, artifact_id)
    data = artifact.get('data') if isinstance(artifact, dict) else None
    if not isinstance(data, dict) or data.get('kind') != 'browser_screenshot':
        raise ValueError('Artifact is not a browser screenshot')
    mime = data.get('content_type')
    if mime not in _TYPES:
        raise ValueError('Browser screenshot content type is invalid')
    root = _root(owner, team_id)
    path = (root / (artifact_id + _TYPES[mime][0])).resolve()
    if path.parent != root or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('Browser screenshot content is unavailable')
    with open(path, 'rb') as handle:
        if not handle.read(len(_TYPES[mime][1])).startswith(_TYPES[mime][1]):
            raise ValueError('Browser screenshot content is invalid')
    return path, mime
