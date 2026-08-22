import asyncio
import hashlib
import os
import re
import shlex
import shutil
import uuid
from pathlib import Path

from langchain_core.tools import tool

from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.tools import (
    ensure_sandbox_initialized,
    ensure_sandbox_initialized_async,
    is_local_sandbox,
)
from deerflow.task_graph.factory import create_task_graph
from deerflow.tools.types import Runtime

VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
WINDOWS_ABSOLUTE_PATH = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<path>.*)$")


class ReviewSnapshotError(RuntimeError):
    """无法从 Implementer 工作现场生成稳定审查快照。"""


class WorktreeCheckpointError(RuntimeError):
    """无法创建或恢复 Fix 前的 Worktree checkpoint。"""


def validate_worktree_name(name: str) -> None:
    """校验 Worktree 名称只能作为单个安全目录名使用。"""
    if name in {"", ".", ".."} or VALID_WORKTREE_NAME.fullmatch(name) is None:
        raise ValueError("invalid worktree name")


def _runtime_thread_id(runtime: Runtime) -> str:
    context = runtime.context or {}
    thread_id = context.get("thread_id")
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id")
    if thread_id is None:
        raise ValueError("thread_id is required")
    return thread_id


def _translate_windows_path_for_wsl(repository_path: str) -> str:
    """在 WSL 宿主中把用户输入的 Windows 绝对路径转为对应挂载路径。"""
    if os.name == "nt":
        return repository_path
    match = WINDOWS_ABSOLUTE_PATH.fullmatch(repository_path)
    if match is None:
        return repository_path
    relative_path = match.group("path").replace("\\", "/").lstrip("/")
    return f"/mnt/{match.group('drive').lower()}/{relative_path}"


def _resolve_repository(repository_path: str) -> Path:
    """解析用户本次选择的本地仓库绝对路径。"""
    requested = Path(_translate_windows_path_for_wsl(repository_path))
    if not requested.is_absolute():
        raise ValueError("repository_path must be an absolute host path")
    resolved_repository = requested.resolve()
    if not resolved_repository.is_dir():
        raise FileNotFoundError(f"repository_path does not exist or is not a directory: {repository_path}")
    return resolved_repository


async def _ensure_worktrees_ignored(sandbox, repository_path: str) -> None:
    """把 Worktree 根目录加入目标仓库的本地排除文件。"""
    quoted_repository = shlex.quote(repository_path)
    exclude_path_output = await asyncio.to_thread(
        sandbox.execute_command,
        f"git -C {quoted_repository} rev-parse --path-format=absolute --git-path info/exclude",
    )
    exclude_path = exclude_path_output.strip()
    if not exclude_path or "\n" in exclude_path:
        actual = exclude_path or "<empty>"
        raise RuntimeError(f"failed to resolve Git info/exclude path: expected one path, got {actual!r}")

    try:
        existing = await asyncio.to_thread(sandbox.read_file, exclude_path)
    except OSError:
        existing = ""
    if ".worktrees/" in {line.strip() for line in existing.splitlines()}:
        return

    separator = "" if not existing or existing.endswith("\n") else "\n"
    await asyncio.to_thread(
        sandbox.write_file,
        exclude_path,
        f"{separator}.worktrees/\n",
        True,
    )


def _copy_untracked_files(source_worktree: Path, snapshot: Path, untracked_output: str) -> None:
    """把 Git diff 未覆盖的未跟踪文件复制到审查快照。"""
    source_root = source_worktree.resolve()
    snapshot_root = snapshot.resolve()
    for raw_path in filter(None, untracked_output.split("\0")):
        relative_path = Path(raw_path)
        source_file = (source_root / relative_path).resolve()
        destination_file = (snapshot_root / relative_path).resolve()
        if not source_file.is_relative_to(source_root) or not destination_file.is_relative_to(snapshot_root):
            raise ReviewSnapshotError("untracked file escaped review snapshot root")
        if not source_file.is_file():
            raise ReviewSnapshotError(f"untracked path is not a file: {raw_path}")
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)


def _validate_checkpoint_path(source: Path, checkpoint: Path) -> None:
    checkpoints_root = (source.parent / ".checkpoints").resolve()
    if not checkpoint.is_relative_to(checkpoints_root):
        raise WorktreeCheckpointError("checkpoint is outside the worktree checkpoint root")


def create_worktree_checkpoint(source_worktree: str, checkpoint_task_id: str, runtime: Runtime) -> str:
    """在修复开始前保存 Worktree 现场，供失败后经人工确认恢复。"""
    sandbox = ensure_sandbox_initialized(runtime)
    if not is_local_sandbox(runtime):
        raise WorktreeCheckpointError("worktree checkpoints currently require LocalSandboxProvider")

    source = Path(source_worktree).resolve()
    if not source.is_dir():
        raise WorktreeCheckpointError(f"source worktree does not exist: {source_worktree}")
    checkpoint_id = f"checkpoint-{hashlib.sha256(checkpoint_task_id.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:8]}"
    checkpoints_root = source.parent / ".checkpoints"
    patches_root = source.parent / ".checkpoint-patches"
    checkpoint = checkpoints_root / checkpoint_id
    patch = patches_root / f"{checkpoint_id}.patch"
    checkpoints_root.mkdir(parents=True, exist_ok=True)
    patches_root.mkdir(parents=True, exist_ok=True)
    quoted_source = shlex.quote(source.as_posix())
    quoted_checkpoint = shlex.quote(checkpoint.as_posix())

    if sandbox.execute_command(f"git -C {quoted_source} rev-parse --is-inside-work-tree").strip() != "true":
        raise WorktreeCheckpointError("source is not a Git worktree")
    diff = sandbox.execute_command(f"git -C {quoted_source} diff --binary HEAD")
    untracked = sandbox.execute_command(f"git -C {quoted_source} ls-files --others --exclude-standard -z")
    sandbox.execute_command(f"git -C {quoted_source} worktree add --detach {quoted_checkpoint} HEAD")
    if sandbox.execute_command(f"git -C {quoted_checkpoint} rev-parse --is-inside-work-tree").strip() != "true":
        raise WorktreeCheckpointError("checkpoint worktree verification failed")
    if diff:
        sandbox.write_file(str(patch), diff, False)
        apply_output = sandbox.execute_command(f"git -C {quoted_checkpoint} apply --whitespace=nowarn {shlex.quote(patch.as_posix())}")
        if "error:" in apply_output.lower() or "fatal:" in apply_output.lower():
            raise WorktreeCheckpointError(f"failed to apply checkpoint diff: {apply_output.strip()}")
    _copy_untracked_files(source, checkpoint, untracked)
    return str(checkpoint)


def restore_worktree_checkpoint(source_worktree: str, checkpoint_path: str, runtime: Runtime) -> None:
    """清除失败 Fix 的改动，并从已验证的 checkpoint 恢复 Worktree。"""
    sandbox = ensure_sandbox_initialized(runtime)
    if not is_local_sandbox(runtime):
        raise WorktreeCheckpointError("worktree checkpoints currently require LocalSandboxProvider")

    source = Path(source_worktree).resolve()
    checkpoint = Path(checkpoint_path).resolve()
    if not source.is_dir() or not checkpoint.is_dir():
        raise WorktreeCheckpointError("source worktree or checkpoint does not exist")
    _validate_checkpoint_path(source, checkpoint)
    quoted_source = shlex.quote(source.as_posix())
    quoted_checkpoint = shlex.quote(checkpoint.as_posix())
    if sandbox.execute_command(f"git -C {quoted_source} rev-parse --is-inside-work-tree").strip() != "true":
        raise WorktreeCheckpointError("source is not a Git worktree")
    if sandbox.execute_command(f"git -C {quoted_checkpoint} rev-parse --is-inside-work-tree").strip() != "true":
        raise WorktreeCheckpointError("checkpoint is not a Git worktree")

    diff = sandbox.execute_command(f"git -C {quoted_checkpoint} diff --binary HEAD")
    untracked = sandbox.execute_command(f"git -C {quoted_checkpoint} ls-files --others --exclude-standard -z")
    reset_output = sandbox.execute_command(f"git -C {quoted_source} reset --hard HEAD")
    clean_output = sandbox.execute_command(f"git -C {quoted_source} clean -fd")
    if "error:" in reset_output.lower() or "fatal:" in reset_output.lower() or "error:" in clean_output.lower() or "fatal:" in clean_output.lower():
        raise WorktreeCheckpointError("failed to clear worktree before restore")
    if diff:
        patch = source.parent / ".checkpoint-patches" / f"restore-{uuid.uuid4().hex[:8]}.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        sandbox.write_file(str(patch), diff, False)
        apply_output = sandbox.execute_command(f"git -C {quoted_source} apply --whitespace=nowarn {shlex.quote(patch.as_posix())}")
        if "error:" in apply_output.lower() or "fatal:" in apply_output.lower():
            raise WorktreeCheckpointError(f"failed to restore checkpoint diff: {apply_output.strip()}")
    _copy_untracked_files(checkpoint, source, untracked)


async def create_review_snapshot(source_worktree: str, implementation_task_id: str, runtime: Runtime) -> str:
    """从 Implementer 的当前未提交现场创建稳定、独立的 Git 审查快照。"""
    sandbox = await ensure_sandbox_initialized_async(runtime)
    if not is_local_sandbox(runtime):
        raise ReviewSnapshotError("review snapshots currently require LocalSandboxProvider")

    source = Path(source_worktree).resolve()
    if not source.is_dir():
        raise ReviewSnapshotError(f"source worktree does not exist: {source_worktree}")
    snapshot_id = f"review-{hashlib.sha256(implementation_task_id.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:8]}"
    snapshots_root = source.parent / ".review-snapshots"
    patches_root = source.parent / ".review-patches"
    snapshot = snapshots_root / snapshot_id
    patch = patches_root / f"{snapshot_id}.patch"
    await asyncio.to_thread(snapshots_root.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(patches_root.mkdir, parents=True, exist_ok=True)
    command_source = source.as_posix()
    command_snapshot = snapshot.as_posix()
    quoted_source = shlex.quote(command_source)
    quoted_snapshot = shlex.quote(command_snapshot)

    is_worktree = await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_source} rev-parse --is-inside-work-tree")
    if is_worktree.strip() != "true":
        raise ReviewSnapshotError(f"source is not a Git worktree: {is_worktree.strip() or '<empty>'}")

    diff = await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_source} diff --binary HEAD")
    untracked = await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_source} ls-files --others --exclude-standard -z")
    await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_source} worktree add --detach {quoted_snapshot} HEAD")
    snapshot_check = await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_snapshot} rev-parse --is-inside-work-tree")
    if snapshot_check.strip() != "true":
        raise ReviewSnapshotError(f"review snapshot verification failed: {snapshot_check.strip() or '<empty>'}")

    if diff:
        await asyncio.to_thread(sandbox.write_file, str(patch), diff, False)
        apply_output = await asyncio.to_thread(sandbox.execute_command, f"git -C {quoted_snapshot} apply --whitespace=nowarn {shlex.quote(patch.as_posix())}")
        if "error:" in apply_output.lower() or "fatal:" in apply_output.lower():
            raise ReviewSnapshotError(f"failed to apply implementation diff: {apply_output.strip()}")
    await asyncio.to_thread(_copy_untracked_files, source, snapshot, untracked)
    return str(snapshot)


@tool("create_coding_worktree", parse_docstring=True)
async def create_coding_worktree(repository_path: str, name: str, task_ids: list[str], runtime: Runtime) -> str:
    """在用户本次选择的本地目标仓库中创建独立 Git Worktree，并绑定持久化任务。

    Args:
        repository_path: 用户本次要修改的本地 Git 仓库绝对路径，可以位于任意磁盘。
        name: Worktree 的安全单段名称，同时用于生成 ``coding/{name}`` 分支。
        task_ids: 需要共享该 Worktree 的持久化 CodingTask ID 列表。
    """
    validate_worktree_name(name)
    if not task_ids:
        raise ValueError("task_ids must not be empty")

    thread_id = _runtime_thread_id(runtime)
    sandbox = await ensure_sandbox_initialized_async(runtime)
    if not is_local_sandbox(runtime):
        raise RuntimeError("host repository worktrees currently require LocalSandboxProvider")

    repository = _resolve_repository(repository_path)
    worktree = repository / ".worktrees" / name
    host_repository = str(repository)
    host_worktree = str(worktree)
    command_repository = repository.as_posix()
    command_worktree = worktree.as_posix()
    branch = f"coding/{name}"
    quoted_repository = shlex.quote(command_repository)
    quoted_worktree = shlex.quote(command_worktree)

    workspace_check = await asyncio.to_thread(
        sandbox.execute_command,
        f"git -C {quoted_repository} rev-parse --is-inside-work-tree",
    )
    workspace_result = workspace_check.strip()
    if workspace_result != "true":
        actual = workspace_result or "<empty>"
        raise RuntimeError(f"target repository is not a Git repository: expected 'true', got {actual!r}")

    await _ensure_worktrees_ignored(sandbox, command_repository)
    create_output = await asyncio.to_thread(
        sandbox.execute_command,
        f"git -C {quoted_repository} worktree add -b {branch} {quoted_worktree} HEAD",
    )
    worktree_check = await asyncio.to_thread(
        sandbox.execute_command,
        f"git -C {quoted_worktree} rev-parse --is-inside-work-tree",
    )
    worktree_branch = await asyncio.to_thread(
        sandbox.execute_command,
        f"git -C {quoted_worktree} branch --show-current",
    )
    if worktree_check.strip() != "true" or worktree_branch.strip() != branch:
        create_detail = create_output.strip() or "<empty>"
        raise RuntimeError(
            f"worktree verification failed: expected path={host_worktree!r}, branch={branch!r}; actual inside-work-tree={worktree_check.strip()!r}, branch={worktree_branch.strip()!r}; git worktree add output={create_detail!r}"
        )

    user_id = resolve_runtime_user_id(runtime)
    graph = create_task_graph(thread_id, user_id=user_id)
    await asyncio.to_thread(graph.bind_worktree, task_ids, host_worktree)

    return f"Created coding worktree '{name}' for {host_repository} at {host_worktree} on branch {branch}; bound {len(task_ids)} tasks"
