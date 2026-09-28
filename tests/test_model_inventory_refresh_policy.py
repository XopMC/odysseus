from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_page_startup_does_not_fetch_endpoint_model_inventory():
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    startup = app.split("// Non-critical startup work", 1)[1].split(
        "runNonCriticalStartup(() => ragModule.loadPersonalDocs()", 1
    )[0]

    assert "modelsModule.refreshModels" not in startup
    assert "sessionModule.updateModelPicker()" in startup


def test_chat_model_picker_has_an_explicit_refresh_path():
    picker = (ROOT / "static/js/modelPicker.js").read_text(encoding="utf-8")

    assert "_refreshPickerModels({ force: true" in picker
