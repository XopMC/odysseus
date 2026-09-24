import asyncio

from src.agent_tools.subprocess_tools import PythonTool
from src import tool_execution


def test_python_tool_explains_missing_stdout_without_changing_printed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tmp_path))
    tool = PythonTool()

    bare = asyncio.run(tool.execute("2 + 2", {}))
    printed = asyncio.run(tool.execute("print(2 + 2)", {}))

    assert bare["exit_code"] == 0
    assert "use print(...)" in bare["output"]
    assert printed == {"output": "4", "exit_code": 0}


def test_python_tool_explains_that_variables_do_not_survive_between_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tmp_path))
    tool = PythonTool()

    assert asyncio.run(tool.execute("part = 42", {}))["exit_code"] == 0
    missing = asyncio.run(tool.execute("print(part)", {}))

    assert missing["exit_code"] != 0
    assert "NameError" in missing["output"]
    assert "new process" in missing["output"].lower()
    assert "same call" in missing["output"].lower()
