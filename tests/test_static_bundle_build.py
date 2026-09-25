from pathlib import Path

import pytest

from scripts.build_static_bundle import build, source_digest


def test_asset_revision_follows_nested_source_and_keeps_source_untouched(tmp_path):
    source = tmp_path / "source"
    (source / "js").mkdir(parents=True)
    (source / "index.html").write_text(
        '<script type="module" src="/static/app.js?v=manual"></script>\n',
        encoding="utf-8",
    )
    (source / "app.js").write_text(
        "import './js/child.js?v=old';\nimport('./js/child.js');\n",
        encoding="utf-8",
    )
    (source / "js/child.js").write_text("export const value = 1;\n", encoding="utf-8")
    (source / "sw.js").write_text(
        "const CACHE_NAME = 'manual';\nconst PRECACHE = ['/static/app.js?v=old'];\n",
        encoding="utf-8",
    )
    original = (source / "app.js").read_bytes()
    first = build(source, tmp_path / "first")
    token = first["asset_revision"]
    assert first["source_sha256"] == source_digest(source)
    assert f'/static/app.js?v={token}' in (tmp_path / "first/index.html").read_text()
    assert (tmp_path / "first/app.js").read_text().count(f'./js/child.js?v={token}') == 2
    worker = (tmp_path / "first/sw.js").read_text()
    assert f"const CACHE_NAME = 'odysseus-{token}'" in worker
    assert f'/static/app.js?v={token}' in worker
    assert (source / "app.js").read_bytes() == original

    (source / "js/child.js").write_text("export const value = 2;\n", encoding="utf-8")
    second = build(source, tmp_path / "second")
    assert second["asset_revision"] != token
    assert f'/static/app.js?v={second["asset_revision"]}' in (tmp_path / "second/index.html").read_text()


def test_asset_bundle_rejects_nested_or_existing_output(tmp_path):
    source = tmp_path / "static"
    source.mkdir()
    (source / "sw.js").write_text("const CACHE_NAME = 'dev';\n")
    with pytest.raises(ValueError):
        build(source, source / "generated")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        build(source, existing)


def test_release_image_serves_generated_bundle_not_source():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    app = (root / "app.py").read_text()
    assert "build_static_bundle.py --source static --output static_build" in dockerfile
    assert "ODYSSEUS_STATIC_DIR=/app/static_build" in dockerfile
    assert 'abs_join(STATIC_DIR, "index.html")' in app
