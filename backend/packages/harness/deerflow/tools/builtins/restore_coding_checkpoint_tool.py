"""在用户确认后，将失败的 Fix Worktree 恢复到修复前 checkpoint。"""

from langchain_core.tools import tool

from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.task_graph.factory import create_task_graph
from deerflow.task_graph.models import TaskStatus
from deerflow.tools.builtins.recover_coding_task_tool import (
    _has_matching_approval,
    _runtime_messages,
)
from deerflow.tools.builtins.worktree_tool import restore_worktree_checkpoint
from deerflow.tools.types import Runtime

RESTORE_CHECKPOINT_OPTION = "Restore checkpoint and stop"
ROLLBACK_CONTEXT_PREFIX = "coding_task_rollback:"


def _runtime_thread_id(runtime: Runtime) -> str:
    context = runtime.context or {}
    thread_id = context.get("thread_id")
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id")
    if thread_id is None:
        raise ValueError("thread_id is required")
    return thread_id


@tool("restore_coding_checkpoint", parse_docstring=True)
def restore_coding_checkpoint(coding_task_id: str, runtime: Runtime) -> str:
    """在用户明确确认后，丢弃失败 Fix 的改动并恢复其修复前 Worktree checkpoint。

    Args:
        coding_task_id: 需要回退的失败 CodingTask ID。
    """
    if not _has_matching_approval(
        _runtime_messages(runtime),
        expected_context=f"{ROLLBACK_CONTEXT_PREFIX}{coding_task_id}",
        expected_option_value=RESTORE_CHECKPOINT_OPTION,
    ):
        raise ValueError("matching checkpoint restore approval is required")

    graph = create_task_graph(_runtime_thread_id(runtime), user_id=resolve_runtime_user_id(runtime))
    task = graph.store.load(coding_task_id)
    if task.status is not TaskStatus.failed:
        raise ValueError("only a failed coding task can restore its checkpoint")
    if not task.worktree or not task.rollback_snapshot:
        raise ValueError("failed coding task has no rollback checkpoint")

    restore_worktree_checkpoint(task.worktree, task.rollback_snapshot, runtime)
    return f"Restored checkpoint {task.rollback_snapshot} for failed task {task.id}; task remains failed"
