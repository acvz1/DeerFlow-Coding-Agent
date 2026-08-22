from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from deerflow.task_graph.models import CodingTask, TaskStatus
from deerflow.tools.builtins.restore_coding_checkpoint_tool import (
    RESTORE_CHECKPOINT_OPTION,
    restore_coding_checkpoint,
)


def _runtime(messages: list) -> SimpleNamespace:
    return SimpleNamespace(
        context={"thread_id": "thread-1"},
        config={"configurable": {}},
        state={"messages": messages},
    )


def _approval_messages(task_id: str, *, option: str = RESTORE_CHECKPOINT_OPTION) -> list:
    request_id = "request-1"
    return [
        ToolMessage(
            id=request_id,
            content="Restore checkpoint?",
            tool_call_id="call-1",
            name="ask_clarification",
            artifact={
                "human_input": {
                    "version": 1,
                    "kind": "human_input_request",
                    "source": "ask_clarification",
                    "request_id": request_id,
                    "clarification_type": "risk_confirmation",
                    "context": f"coding_task_rollback:{task_id}",
                    "options": [{"id": "option-1", "value": RESTORE_CHECKPOINT_OPTION}],
                }
            },
        ),
        HumanMessage(
            content=option,
            additional_kwargs={
                "human_input_response": {
                    "version": 1,
                    "kind": "human_input_response",
                    "source": "ask_clarification",
                    "request_id": request_id,
                    "response_kind": "option",
                    "option_id": "option-1",
                    "value": option,
                }
            },
        ),
    ]


def test_restore_coding_checkpoint_requires_approval_and_restores_failed_fix(monkeypatch):
    task = CodingTask(
        id="coding-review-fix",
        subject="Fix",
        description="Fix review findings",
        status=TaskStatus.failed,
        worktree="D:/repo/.worktrees/coding-run",
        rollback_snapshot="D:/repo/.worktrees/.checkpoints/checkpoint-1",
    )
    calls: list[tuple] = []
    graph = SimpleNamespace(store=SimpleNamespace(load=lambda task_id: calls.append(("load", task_id)) or task))

    monkeypatch.setattr(
        "deerflow.tools.builtins.restore_coding_checkpoint_tool.create_task_graph",
        lambda thread_id, *, user_id: calls.append(("graph", thread_id, user_id)) or graph,
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.restore_coding_checkpoint_tool.resolve_runtime_user_id",
        lambda _runtime: "alice",
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.restore_coding_checkpoint_tool.restore_worktree_checkpoint",
        lambda worktree, checkpoint, runtime: calls.append(("restore", worktree, checkpoint, runtime)),
    )

    runtime = _runtime(_approval_messages(task.id))
    result = restore_coding_checkpoint.func(coding_task_id=task.id, runtime=runtime)

    assert calls == [
        ("graph", "thread-1", "alice"),
        ("load", "coding-review-fix"),
        ("restore", task.worktree, task.rollback_snapshot, runtime),
    ]
    assert "task remains failed" in result


def test_restore_coding_checkpoint_rejects_missing_approval(monkeypatch):
    monkeypatch.setattr(
        "deerflow.tools.builtins.restore_coding_checkpoint_tool.create_task_graph",
        lambda *_args, **_kwargs: pytest.fail("graph must not be opened"),
    )

    with pytest.raises(ValueError, match="matching checkpoint restore approval"):
        restore_coding_checkpoint.func(coding_task_id="fix-1", runtime=_runtime([]))
