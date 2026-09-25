"""Build a deterministic, automatically versioned copy of the static app.

The source tree keeps readable import paths for local development.  Release
images serve this generated copy: every local JS/CSS URL gets a query token
derived from the *whole* source asset graph, and the service worker receives
the same cache revision.  A bundle-wide digest (rather than independent file
hashes) also invalidates importers when a deeply imported module changes.

The server continues to revalidate text assets.  Query versions are not treated
as immutable URLs because old tabs may request a lazy module after a deploy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


ASSET_URL = re.compile(
    r"(?P<path>(?:/static/|\.\.?/)[A-Za-z0-9_./-]+\.(?:js|css))"
    r"(?:\?v=[A-Za-z0-9_.-]+)?"
)
TEXT_SUFFIXES = frozenset({".js", ".css", ".html"})


def source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        relative = path.relative_to(source).as_posix()
        if relative == "asset-build.json":
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    output = output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("The generated bundle must be outside the source tree")
    if output.exists():
        raise FileExistsError(f"Generated bundle already exists: {output}")
    full_digest = source_digest(source)
    token = full_digest[:20]
    shutil.copytree(source, output, symlinks=False)
    rewritten = 0
    for path in sorted(p for p in output.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
        original = path.read_text(encoding="utf-8")
        text = ASSET_URL.sub(lambda match: f"{match.group('path')}?v={token}", original)
        if path.relative_to(output).as_posix() == "sw.js":
            text, count = re.subn(
                r"(?m)^const CACHE_NAME = ['\"][^'\"]+['\"];",
                f"const CACHE_NAME = 'odysseus-{token}';",
                text,
                count=1,
            )
            if count != 1:
                raise ValueError("Service worker cache name declaration missing")
        if text != original:
            path.write_text(text, encoding="utf-8")
            rewritten += 1
    manifest = {
        "source_sha256": full_digest,
        "asset_revision": token,
        "rewritten_files": rewritten,
    }
    (output / "asset-build.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
