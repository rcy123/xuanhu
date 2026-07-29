from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from app.agent_runtime.errors import CheckpointConfigMismatchError, GraphRunnerError
from app.agent_runtime.runner import GraphRunner


class _State(TypedDict, total=False):
    session_id: str
    graph_version: str
    answer_ref: str


async def _interrupt_node(state: _State) -> dict[str, str]:
    value = interrupt({"kind": "test", "request_ref": "request-1"})
    return {"answer_ref": value["answer_ref"]}


def _graph() -> GraphRunner:
    builder = StateGraph(_State)
    builder.add_node("interrupt", _interrupt_node)
    builder.add_edge(START, "interrupt")
    builder.add_edge("interrupt", END)
    return GraphRunner(builder.compile(checkpointer=InMemorySaver()), timeout_seconds=2)


@pytest.mark.asyncio
async def test_runner_resumes_same_checkpoint_with_reference_only_command() -> None:
    runner = _graph()
    config = {"configurable": {"thread_id": "v1:11111111-1111-1111-1111-111111111111"}}
    initial = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "graph_version": "v1",
    }
    interrupted = await runner.ainvoke(initial, config=config)
    assert "answer_ref" not in interrupted

    resumed = await runner.aresume(
        session_id=initial["session_id"],
        graph_version="v1",
        resume={"answer_ref": "submission-1"},
        config=config,
    )
    assert resumed["answer_ref"] == "submission-1"


@pytest.mark.asyncio
async def test_runner_rejects_resume_for_other_checkpoint_namespace() -> None:
    runner = _graph()
    with pytest.raises(CheckpointConfigMismatchError):
        await runner.aresume(
            session_id="11111111-1111-1111-1111-111111111111",
            graph_version="v1",
            resume={"answer_ref": "submission-1"},
            config={"configurable": {"thread_id": "v1:22222222-2222-2222-2222-222222222222"}},
        )


@pytest.mark.asyncio
async def test_runner_rejects_non_reference_resume_payload() -> None:
    runner = _graph()
    with pytest.raises(GraphRunnerError, match="string references"):
        await runner.aresume(
            session_id="11111111-1111-1111-1111-111111111111",
            graph_version="v1",
            resume={"answer_ref": 1},  # type: ignore[dict-item]
            config={"configurable": {"thread_id": "v1:11111111-1111-1111-1111-111111111111"}},
        )
