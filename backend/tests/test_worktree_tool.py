import shlex
from types import SimpleNamespace

import pytest

import deerflow.tools.builtins.worktree_tool as worktree_module
from deerflow.tools.builtins.worktree_tool import (
    _resolve_repository,
    _translate_windows_path_for_wsl,
    create_coding_worktree,
    create_review_snapshot,
    create_worktree_checkpoint,
    restore_worktree_checkpoint,
    validate_worktree_name,
)


def test_translate_windows_path_for_wsl_preserves_native_windows_host(monkeypatch):
    monkeypatch.setattr(worktree_module.os, "name", "nt")

    assert _translate_windows_path_for_wsl(r"D:\Project\campus-trade-platform") == r"D:\Project\campus-trade-platform"


def test_translate_windows_path_for_wsl_maps_drive_and_backslashes(monkeypatch):
    monkeypatch.setattr(worktree_module.os, "name", "posix")

    assert _translate_windows_path_for_wsl(r"D:\Project\campus-trade-platform") == "/mnt/d/Project/campus-trade-platform"


def test_translate_windows_path_for_wsl_leaves_posix_path_unchanged(monkeypatch):
    monkeypatch.setattr(worktree_module.os, "name", "posix")

    assert _translate_windows_path_for_wsl("/mnt/d/Project/campus-trade-platform") == "/mnt/d/Project/campus-trade-platform"


def test_resolve_repository_translates_windows_path_before_validating(monkeypatch, tmp_path):
    resolved_repository = tmp_path / "campus-trade-platform"
    resolved_repository.mkdir()
    requested_paths: list[str] = []

    def fake_path(path: str):
        requested_paths.append(path)
        return resolved_repository

    monkeypatch.setattr(worktree_module.os, "name", "posix")
    monkeypatch.setattr(worktree_module, "Path", fake_path)

    assert _resolve_repository(r"D:\Project\campus-trade-platform") == resolved_repository.resolve()
    assert requested_paths == ["/mnt/d/Project/campus-trade-platform"]


@pytest.mark.parametrize("name", ["coding-run", "coding_run.1", "A1"])
def test_validate_worktree_name_accepts_safe_single_segment_names(name):
    assert validate_worktree_name(name) is None


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../escape", "nested/path", "nested\\path", "has space", "a" * 65],
)
def test_validate_worktree_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError, match="worktree name"):
        validate_worktree_name(name)


class FakeSandbox:
    def __init__(self, worktree_branch: str = "coding/coding-run"):
        self.worktree_branch = worktree_branch
        self.commands: list[str] = []
        self.files = {"/git/info/exclude": "# repository exclusions\n"}
        self.writes: list[tuple[str, str, bool]] = []

    def execute_command(self, command: str) -> str:
        self.commands.append(command)
        if command.endswith("rev-parse --is-inside-work-tree"):
            return "true\n"
        if command.endswith("rev-parse --path-format=absolute --git-path info/exclude"):
            return "/git/info/exclude\n"
        if command.endswith("branch --show-current"):
            return f"{self.worktree_branch}\n"
        if "worktree add" in command:
            return "Preparing worktree"
        raise AssertionError(f"unexpected command: {command}")

    def read_file(self, path: str) -> str:
        return self.files[path]

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.writes.append((path, content, append))
        if append:
            self.files[path] = self.files.get(path, "") + content
        else:
            self.files[path] = content


def _runtime(thread_id: str | None = "thread-1") -> SimpleNamespace:
    context = {} if thread_id is None else {"thread_id": thread_id}
    return SimpleNamespace(context=context, config={"configurable": {}}, state={})


@pytest.mark.anyio
async def test_create_coding_worktree_uses_user_selected_target_repository_then_binds(monkeypatch, tmp_path):
    events: list[tuple] = []
    repository = tmp_path / "project-a"
    repository.mkdir()
    repository = repository.resolve()
    worktree = repository / ".worktrees" / "coding-run"
    command_repository = repository.as_posix()
    command_worktree = worktree.as_posix()
    sandbox = FakeSandbox()

    class FakeGraph:
        def bind_worktree(self, task_ids, worktree_path):
            events.append(("bind", task_ids, worktree_path))
            return []

    async def fake_ensure_sandbox(runtime):
        events.append(("sandbox", runtime.context["thread_id"]))
        return sandbox

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized_async", fake_ensure_sandbox)
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)
    monkeypatch.setattr(worktree_module, "resolve_runtime_user_id", lambda _runtime: "alice")
    monkeypatch.setattr(worktree_module, "create_task_graph", lambda _thread_id, *, user_id: FakeGraph())

    result = await create_coding_worktree.coroutine(
        repository_path=str(repository),
        name="coding-run",
        task_ids=["coding-analysis", "coding-implementation", "coding-review"],
        runtime=_runtime(),
    )

    assert sandbox.commands == [
        f"git -C {shlex.quote(command_repository)} rev-parse --is-inside-work-tree",
        f"git -C {shlex.quote(command_repository)} rev-parse --path-format=absolute --git-path info/exclude",
        f"git -C {shlex.quote(command_repository)} worktree add -b coding/coding-run {shlex.quote(command_worktree)} HEAD",
        f"git -C {shlex.quote(command_worktree)} rev-parse --is-inside-work-tree",
        f"git -C {shlex.quote(command_worktree)} branch --show-current",
    ]
    assert sandbox.writes == [("/git/info/exclude", ".worktrees/\n", True)]
    assert events == [
        ("sandbox", "thread-1"),
        (
            "bind",
            ["coding-analysis", "coding-implementation", "coding-review"],
            str(worktree),
        ),
    ]
    assert result == (f"Created coding worktree 'coding-run' for {repository} at {worktree} on branch coding/coding-run; bound 3 tasks")


@pytest.mark.anyio
async def test_create_coding_worktree_does_not_bind_when_worktree_verification_fails(monkeypatch, tmp_path):
    repository = tmp_path / "project-a"
    repository.mkdir()
    sandbox = FakeSandbox(worktree_branch="main")

    class FakeGraph:
        def bind_worktree(self, task_ids, worktree_path):
            raise AssertionError("tasks must not bind before Git verification")

    async def fake_ensure_sandbox(_runtime):
        return sandbox

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized_async", fake_ensure_sandbox)
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)
    monkeypatch.setattr(worktree_module, "resolve_runtime_user_id", lambda _runtime: "alice")
    monkeypatch.setattr(worktree_module, "create_task_graph", lambda _thread_id, *, user_id: FakeGraph())

    with pytest.raises(RuntimeError, match="verification failed") as exc_info:
        await create_coding_worktree.coroutine(
            repository_path=str(repository),
            name="coding-run",
            task_ids=["coding-analysis"],
            runtime=_runtime(),
        )

    message = str(exc_info.value)
    assert f"expected path={str(repository.resolve() / '.worktrees' / 'coding-run')!r}" in message
    assert "branch='coding/coding-run'" in message
    assert "actual inside-work-tree='true', branch='main'" in message
    assert "git worktree add output='Preparing worktree'" in message


@pytest.mark.anyio
async def test_create_coding_worktree_reports_actual_repository_check_output(monkeypatch, tmp_path):
    repository = tmp_path / "project-a"
    repository.mkdir()
    sandbox = FakeSandbox()

    async def fake_ensure_sandbox(_runtime):
        return sandbox

    original_execute = sandbox.execute_command

    def execute_command(command: str) -> str:
        if command.endswith("rev-parse --is-inside-work-tree"):
            return "fatal: not a git repository\nExit Code: 128"
        return original_execute(command)

    sandbox.execute_command = execute_command
    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized_async", fake_ensure_sandbox)
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)

    with pytest.raises(RuntimeError, match="fatal: not a git repository"):
        await create_coding_worktree.coroutine(
            repository_path=str(repository),
            name="coding-run",
            task_ids=["coding-analysis"],
            runtime=_runtime(),
        )


@pytest.mark.anyio
async def test_create_coding_worktree_rejects_non_local_sandbox(monkeypatch, tmp_path):
    async def fake_ensure_sandbox(_runtime):
        return FakeSandbox()

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized_async", fake_ensure_sandbox)
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: False)

    with pytest.raises(RuntimeError, match="LocalSandboxProvider"):
        await create_coding_worktree.coroutine(
            repository_path=str(tmp_path),
            name="coding-run",
            task_ids=["coding-analysis"],
            runtime=_runtime(),
        )


@pytest.mark.anyio
async def test_ensure_worktrees_ignored_reports_actual_invalid_git_path_output():
    sandbox = FakeSandbox()
    original_execute = sandbox.execute_command

    def execute_command(command: str) -> str:
        if command.endswith("rev-parse --path-format=absolute --git-path info/exclude"):
            return "fatal: cannot resolve git path\nExit Code: 128"
        return original_execute(command)

    sandbox.execute_command = execute_command

    with pytest.raises(RuntimeError, match="fatal: cannot resolve git path"):
        await worktree_module._ensure_worktrees_ignored(sandbox, "E:/project-a")


@pytest.mark.anyio
async def test_create_coding_worktree_requires_thread_id(tmp_path):
    with pytest.raises(ValueError, match="thread_id is required"):
        await create_coding_worktree.coroutine(
            repository_path=str(tmp_path),
            name="coding-run",
            task_ids=["coding-analysis"],
            runtime=_runtime(thread_id=None),
        )


@pytest.mark.anyio
async def test_create_review_snapshot_copies_tracked_diff_into_detached_worktree(monkeypatch, tmp_path):
    source = tmp_path / ".worktrees" / "coding-run"
    source.mkdir(parents=True)
    commands: list[str] = []
    writes: list[tuple[str, str, bool]] = []

    class SnapshotSandbox:
        def execute_command(self, command: str) -> str:
            commands.append(command)
            if command.endswith("rev-parse --is-inside-work-tree"):
                return "true\n"
            if command.endswith("diff --binary HEAD"):
                return "diff --git a/pricing.py b/pricing.py\n"
            if command.endswith("ls-files --others --exclude-standard -z"):
                return ""
            if "worktree add --detach" in command:
                return "Preparing worktree\n"
            if " apply --whitespace=nowarn " in command:
                return ""
            raise AssertionError(f"unexpected command: {command}")

        def write_file(self, path: str, content: str, append: bool = False) -> None:
            writes.append((path, content, append))

    async def fake_ensure_sandbox(_runtime):
        return SnapshotSandbox()

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized_async", fake_ensure_sandbox)
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)
    monkeypatch.setattr(worktree_module.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))

    snapshot = await create_review_snapshot(str(source), "implement-pricing", _runtime())

    expected_name = "review-" + __import__("hashlib").sha256(b"implement-pricing").hexdigest()[:12] + "-12345678"
    expected_snapshot = source.parent / ".review-snapshots" / expected_name
    expected_patch = source.parent / ".review-patches" / f"{expected_name}.patch"
    assert snapshot == str(expected_snapshot)
    assert any("worktree add --detach" in command for command in commands)
    assert writes == [(str(expected_patch), "diff --git a/pricing.py b/pricing.py\n", False)]


def test_create_worktree_checkpoint_copies_current_worktree_state(monkeypatch, tmp_path):
    source = tmp_path / ".worktrees" / "coding-run"
    source.mkdir(parents=True)
    commands: list[str] = []
    writes: list[tuple[str, str, bool]] = []

    class CheckpointSandbox:
        def execute_command(self, command: str) -> str:
            commands.append(command)
            if command.endswith("rev-parse --is-inside-work-tree"):
                return "true\n"
            if command.endswith("diff --binary HEAD"):
                return "diff --git a/pricing.py b/pricing.py\n"
            if command.endswith("ls-files --others --exclude-standard -z"):
                return ""
            if "worktree add --detach" in command or " apply --whitespace=nowarn " in command:
                return ""
            raise AssertionError(f"unexpected command: {command}")

        def write_file(self, path: str, content: str, append: bool = False) -> None:
            writes.append((path, content, append))

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized", lambda _runtime: CheckpointSandbox())
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)
    monkeypatch.setattr(worktree_module.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))

    checkpoint = create_worktree_checkpoint(str(source), "coding-review", _runtime())

    expected_name = "checkpoint-" + __import__("hashlib").sha256(b"coding-review").hexdigest()[:12] + "-12345678"
    expected_checkpoint = source.parent / ".checkpoints" / expected_name
    expected_patch = source.parent / ".checkpoint-patches" / f"{expected_name}.patch"
    assert checkpoint == str(expected_checkpoint)
    assert any("worktree add --detach" in command for command in commands)
    assert writes == [(str(expected_patch), "diff --git a/pricing.py b/pricing.py\n", False)]


def test_restore_worktree_checkpoint_discards_failed_fix_and_reapplies_baseline(monkeypatch, tmp_path):
    source = tmp_path / ".worktrees" / "coding-run"
    checkpoint = source.parent / ".checkpoints" / "checkpoint-1"
    source.mkdir(parents=True)
    checkpoint.mkdir(parents=True)
    commands: list[str] = []
    writes: list[tuple[str, str, bool]] = []

    class RestoreSandbox:
        def execute_command(self, command: str) -> str:
            commands.append(command)
            if command.endswith("rev-parse --is-inside-work-tree"):
                return "true\n"
            if command.endswith("diff --binary HEAD"):
                return "diff --git a/pricing.py b/pricing.py\n"
            if command.endswith("ls-files --others --exclude-standard -z"):
                return ""
            if command.endswith("reset --hard HEAD") or command.endswith("clean -fd"):
                return ""
            if " apply --whitespace=nowarn " in command:
                return ""
            raise AssertionError(f"unexpected command: {command}")

        def write_file(self, path: str, content: str, append: bool = False) -> None:
            writes.append((path, content, append))

    monkeypatch.setattr(worktree_module, "ensure_sandbox_initialized", lambda _runtime: RestoreSandbox())
    monkeypatch.setattr(worktree_module, "is_local_sandbox", lambda _runtime: True)
    monkeypatch.setattr(worktree_module.uuid, "uuid4", lambda: SimpleNamespace(hex="abcdef123456"))

    restore_worktree_checkpoint(str(source), str(checkpoint), _runtime())

    assert any(command.endswith("reset --hard HEAD") for command in commands)
    assert any(command.endswith("clean -fd") for command in commands)
    assert writes == [
        (
            str(source.parent / ".checkpoint-patches" / "restore-abcdef12.patch"),
            "diff --git a/pricing.py b/pricing.py\n",
            False,
        )
    ]
