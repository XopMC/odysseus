import pytest

from src.agent_harness_benchmark import run


@pytest.mark.asyncio
async def test_ab_benchmark_freezes_tasks_and_reports_quality_and_latency():
    tasks = [{"id": "long-1", "prompt": "work"}]
    async def invoke(profile, task):
        task["prompt"] = "mutated"
        return {"input_tokens": 100 if profile == "off" else 70, "output_tokens": 20,
                "requests": 3, "ttft_seconds": .2, "duration_seconds": 2,
                "quality": .9, "completed": True, "evidence": ["ok"]}
    report = await run(tasks, ["off", "efficiency"], invoke=invoke)
    assert tasks[0]["prompt"] == "work"
    assert report["aggregates"]["efficiency"]["input_tokens"] == 70
    assert report["aggregates"]["off"]["mean_quality"] == .9
    assert len(report["task_set_sha256"]) == 64
