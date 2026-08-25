---
name: multi-agent-coding
description: "Run an approved coding, analysis-only, or review-only workflow with specialized subagents."
allowed-tools:
  - ask_clarification
  - submit_task_plan
  - create_coding_worktree
  - recover_coding_task
  - restore_coding_checkpoint
  - continue_after_review
  - task
---

# Multi-Agent Coding

将用户已确认的 `coding_brief` 交给专业子 Agent。先选择工作流，不得为了凑流程强制执行无关角色。

## Input Gate

1. 确认 `coding_brief` 至少包含 `goal`、`acceptance_criteria`、`tasks`。
2. 确认 `workflow_type`，只允许：
   - `analyze_only`：只分析真实代码；
   - `review_only`：只审查指定代码快照；
   - `implement_and_review`：分析、实现、审查；缺省时使用此值。
3. `review_only` 必须先问清用户要审查的仓库。当前 Worktree 从该仓库已提交的 `HEAD` 创建；未提交改动不属于审查输入。
4. 用户提供的 Windows 绝对路径（如 `D:\\Project\\app`）是有效的仓库输入，不能因为 Lead 自己运行在 Linux/WSL 视角就要求用户换成 `/mnt/...`、上传副本或重新挂载。`create_coding_worktree` 会在 WSL Gateway 中自动完成 `D:\\... -> /mnt/d/...` 映射；应直接保留用户原始路径调用该工具，并以工具实际校验结果为准。
5. 缺字段或 `open_questions` 会影响安全执行时，停止并让用户确认；不得自行补全需求。

## Approval Gate

展示目标、验收标准、工作流类型和目标仓库后，调用 `ask_clarification`：

- `question`: `Approve this coding plan and start the isolated coding run?`
- `clarification_type`: `risk_confirmation`
- `context`: `coding_plan_approval`
- `options`: `Approve coding plan`、`Reject coding plan`

只有匹配该请求的结构化回复明确选择 `Approve coding plan`，才能持久化 DAG、创建 Worktree 或委派子 Agent。

## Persist Task DAG

批准后调用一次 `submit_task_plan(coding_brief, tasks)`；必须传入完整原始 `coding_brief`。服务端会持久化它，使每个新子 Agent 都能重新读取用户目标，而不是依赖 Lead 对话记忆。

小需求按 `workflow_type` 使用以下稳定任务计划。复杂需求则由 Lead 先把 `coding_brief` 拆成范围明确的业务单元，在用户批准后一次性提交完整 DAG；不得在 Analyzer 返回后未经用户批准临时扩任务。

复杂实现需求中，每个业务单元使用 `analysis-{unit} -> implement-{unit} -> review-{unit}`。各 `analysis-*` 可独立准备；每个 `implement-*` 必须依赖自己的 `analysis-*`，并按计划顺序依赖前一个 `implement-*`，保证同一 Worktree 同时只有一个写入者。每个 `review-*` 依赖自己的 `implement-*`；Implementer 完成后，后端会自动生成稳定 Git 审查快照并绑定直接下游 Reviewer，所以 Reviewer 审查上一单元时，下一个 Implementer 可以继续在主 Worktree 工作。

小需求模板如下：

### `analyze_only`

```json
[
  {"id": "coding-analysis", "subject": "Analyze coding brief", "description": "Inspect the real codebase and produce the analysis report.", "blocked_by": [], "agent_type": "code-analyzer"}
]
```

### `review_only`

```json
[
  {"id": "coding-review", "subject": "Review code", "description": "Independently inspect the requested code snapshot and produce the review report.", "blocked_by": [], "agent_type": "code-reviewer"}
]
```

### `implement_and_review`

```json
[
  {"id": "coding-analysis", "subject": "Analyze coding brief", "description": "Inspect the real codebase and produce the analysis report.", "blocked_by": [], "agent_type": "code-analyzer"},
  {"id": "coding-implementation", "subject": "Implement coding brief", "description": "Implement the confirmed change and produce test evidence.", "blocked_by": ["coding-analysis"], "agent_type": "code-implementer"},
  {"id": "coding-review", "subject": "Review implementation", "description": "Independently review the implementation and verification evidence.", "blocked_by": ["coding-implementation"], "agent_type": "code-reviewer"}
]
```

`submit_task_plan` 成功后，调用 `create_coding_worktree`，并把该工作流的全部任务 ID 绑定到同一个 Worktree。只有 Worktree 创建成功，才可委派。

## Delegate Tasks

根据保存的 DAG 依赖顺序调用 `task`：

- `code-analyzer` 必须返回 `analysis_report`；
- `code-implementer` 必须返回 `implementation_report`；
- `code-reviewer` 必须返回 `review_report`。

`task` 会在启动前重新注入 `coding_brief`，并自动递归注入已校验的上游 Artifact。Analyzer 和 Reviewer 是只读角色；Implementer 是唯一可写角色。

`analyze_only` 在 `analysis_report` 合格后完成。`review_only` 无论 `review_report.verdict` 是 PASS 或 FAIL 都表示审查任务已完成；必须如实把 verdict 返回用户。`implement_and_review` 只有最终 Reviewer PASS 才表示修改验收通过。

## Technical Failure Recovery

子 Agent 超时、取消、异常或 Artifact 校验失败时，对应 CodingTask 会变为 `failed`。没有 `rollback_snapshot` 时，调用 `ask_clarification`：

- `clarification_type`: `risk_confirmation`
- `context`: `coding_task_recovery:{coding_task_id}`
- `options`: `Retry failed task`、`Stop pipeline`

只有用户匹配选择 `Retry failed task` 后，才调用 `recover_coding_task` 并重试同一任务。每次重试都需要新的人工确认。

如果失败任务存在 `rollback_snapshot`，说明它属于 Review FAIL 后的 Fix 流程。先展示失败原因和将被丢弃的未提交修改，再额外提供：

- `clarification_type`: `risk_confirmation`
- `context`: `coding_task_rollback:{coding_task_id}`
- `options`: `Restore checkpoint and stop`、`Retry failed task`、`Stop pipeline`

只有用户匹配选择 `Restore checkpoint and stop` 后，才调用 `restore_coding_checkpoint(coding_task_id=...)`。该工具会在隔离 Worktree 中执行清理并恢复 Fix 前的 checkpoint，任务状态仍保持 `failed`，保留失败记录；不得自动回退。

## Review FAIL Follow-up

`review_report.verdict = FAIL` 不是 Reviewer 技术失败，不能调用 `recover_coding_task`。先保留其 Artifact 和 Worktree，向用户展示 `issues` 与 `required_changes`，并询问：

- `question`: `The review failed. Reanalyze the findings, fix them, and run an independent review again?`
- `clarification_type`: `risk_confirmation`
- `context`: `coding_review_followup:{review_task_id}`
- `options`: `Reanalyze and fix`、`Stop pipeline`

只有用户匹配选择 `Reanalyze and fix` 后，才调用 `continue_after_review(review_task_id=...)`。该工具会先为原 Worktree 创建 Fix 前 checkpoint，再在原 Worktree 上追加：

```text
failed review
  -> code-analyzer (reanalyze)
  -> code-implementer (fix)
  -> code-reviewer (rereview)
```

随后按 DAG 顺序委派新增节点。不得自动循环；每次新的 Review FAIL 都必须重新取得人工批准。

## Output Contract

返回一个 `coding_run`：

```yaml
coding_run:
  status: completed | failed | needs_follow_up
  workflow_type: analyze_only | review_only | implement_and_review
  coding_brief: original confirmed brief
  analysis_report: report or null
  implementation_report: report or null
  review_report: report or null
  failure_stage: input | analysis | implementation | review | null
  last_failure_reason: observed reason from the most recent failed attempt, or null; preserved when a task is recovered
```

`review_only` 的 FAIL 是审查结论，不是伪造的成功，也不是技术失败；`implement_and_review` 的 FAIL 应返回 `needs_follow_up`，等待用户停止或批准重新分析后修复。
