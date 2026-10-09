import os
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, Settings
from agent_dispatch.dispatch.task_package import TaskPackage, build_task_package, render_prompt
from agent_dispatch.models import ContextMode, DispatchRequest, SourceAgent


def settings(*, max_children: int = 2) -> Settings:
    return Settings(
        executors={"codex": ExecutorSettings(adapter="codex")},
        routing=RoutingSettings(fallback_executor="codex", max_children=max_children),
    )


def request(mode=ContextMode.prompt_summary, cwd="/repo/project", **kwargs):
    values = dict(
        task="Fix failing test",
        cwd=cwd,
        context="pytest fails after refactor",
        files=["tests/test_x.py", "src/client.py"],
        constraints=["do not change public API"],
        success_criteria=["targeted tests pass"],
        context_mode=mode,
        source_agent=SourceAgent.codex,
    )
    values.update(kwargs)
    return DispatchRequest(**values)


@pytest.mark.parametrize("mode", [ContextMode.prompt, ContextMode.prompt_summary])
def test_build_task_package_without_git(mode):
    package = build_task_package(request(mode), settings())
    assert package.git_status is None
    assert package.git_diff_stat is None


def test_build_task_package_full_collects_git_status_and_diff(git_repo):
    (Path(git_repo) / "a.py").write_text("changed\n")
    package = build_task_package(request(ContextMode.full, str(git_repo)), settings())
    assert package.git_status is not None and "a.py" in package.git_status
    assert package.git_diff_stat is not None and "a.py" in package.git_diff_stat


@pytest.mark.parametrize(
    ("adapter", "mode", "snapshot"),
    [
        ("claude", ContextMode.prompt, "prompt_claude_prompt.md"),
        ("claude", ContextMode.prompt_summary, "prompt_claude_summary.md"),
        ("claude", ContextMode.full, "prompt_claude_full.md"),
        ("codex", ContextMode.prompt_summary, "prompt_codex_summary.md"),
    ],
)
def test_render_prompt_matches_snapshot(adapter, mode, snapshot):
    package = TaskPackage(
        request=request(mode),
        git_status=" M src/client.py\n" if mode is ContextMode.full else None,
        git_diff_stat=" src/client.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        if mode is ContextMode.full
        else None,
    )
    rendered = render_prompt(package, adapter, settings())
    path = Path(__file__).parent / "snapshots" / snapshot
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(rendered)
    assert rendered == path.read_text()


def test_render_prompt_prompt_mode_omits_context_sections():
    rendered = render_prompt(TaskPackage(request=request(ContextMode.prompt)), "claude", settings())
    assert "# Context" not in rendered
    assert "## Relevant files" not in rendered
    assert "## Constraints" not in rendered
    assert "## Success criteria" not in rendered


def test_render_prompt_hop_limit_forbids_delegation():
    rendered = render_prompt(TaskPackage(request=request(hop=1)), "claude", settings())
    assert "Do NOT delegate further" in rendered
    assert "split this task" not in rendered


def test_render_prompt_hop_zero_allows_fanout():
    rendered = render_prompt(TaskPackage(request=request(hop=0)), "claude", settings())
    assert "at most 2 independent subtasks" in rendered


def test_render_prompt_uses_configured_max_children():
    rendered = render_prompt(TaskPackage(request=request()), "claude", settings(max_children=3))
    assert "at most 3 independent subtasks" in rendered


def test_render_prompt_codex_and_claude_only_differ_in_result_format():
    package = TaskPackage(request=request())
    claude = render_prompt(package, "claude", settings())
    codex = render_prompt(package, "codex", settings())
    marker = "# Result format"
    assert claude.split(marker, 1)[0] == codex.split(marker, 1)[0]
    assert claude.split(marker, 1)[1] != codex.split(marker, 1)[1]


def test_render_prompt_full_contains_git_commands_and_fixed_state():
    package = TaskPackage(
        request=request(ContextMode.full),
        git_status=" M src/client.py\n",
        git_diff_stat=" src/client.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
    )
    rendered = render_prompt(package, "claude", settings())
    assert "$ git status --short" in rendered
    assert "$ git diff --stat" in rendered
    assert " M src/client.py\n" in rendered
    assert " src/client.py | 2 +-" in rendered


def test_render_prompt_task_assigns_ownership():
    rendered = render_prompt(TaskPackage(request=request()), "claude", settings())
    assert "# Ownership" in rendered
    assert "You own the implementation, the regression tests and deterministic evals" in rendered
    assert "no repeats of green unchanged checks" in rendered
    assert "no paid evals" in rendered
    assert "The caller owns independent review, the final combined gate and the commit" in rendered


def test_render_prompt_review_ownership_is_findings_only():
    rendered = render_prompt(TaskPackage(request=request(kind="review")), "claude", settings())
    assert "Findings only" in rendered
    assert "the caller fixes, reruns what is needed and commits" in rendered
    assert "Do not rerun green checks unchanged by the diff" in rendered
    assert "You own the implementation" not in rendered


def test_render_prompt_task_ownership_forbids_executor_commit():
    rendered = render_prompt(TaskPackage(request=request()), "claude", settings())
    ownership = rendered.split("# Ownership", 1)[1].split("# Rules", 1)[0]
    assert "commit" not in ownership.replace("final combined gate and the commit", "")


def test_render_prompt_none_context_is_rendered_as_none():
    rendered = render_prompt(TaskPackage(request=request(context=None)), "claude", settings())
    assert "# Context\n(none)" in rendered


def test_render_prompt_empty_lists_render_none_bullets():
    rendered = render_prompt(
        TaskPackage(request=request(files=[], constraints=[], success_criteria=[])),
        "claude",
        settings(),
    )
    assert "## Relevant files\n- (none)" in rendered
    assert "## Constraints\n- (none)" in rendered
    assert "## Success criteria\n- (none)" in rendered


def test_render_prompt_states_time_budget_from_routing_default():
    rendered = render_prompt(TaskPackage(request=request()), "claude", settings())
    assert "# Time budget" in rendered
    assert "time budget is 1800 seconds (~30 minutes)" in rendered
    assert "Do not rerun a long test suite" in rendered
    assert "Stop and report" in rendered


def test_render_prompt_states_requested_timeout():
    package = TaskPackage(request=request(timeout_seconds=600))
    rendered = render_prompt(package, "claude", settings())
    assert "time budget is 600 seconds (~10 minutes)" in rendered


def test_render_prompt_worktree_mode_rewrites_repo_paths():
    req = request(
        cwd="/repo/project",
        task="Read /repo/project/src/client.py and fix the failing test",
        context="pytest fails on /repo/project/tests/test_x.py",
        files=["tests/test_x.py"],
    )
    # Файлы обычно относительные (валидатор запрещает абсолютные), но через
    # model_copy абсолютный путь всё же доходит — его тоже переписываем.
    req = req.model_copy(update={"files": ["/repo/project/tests/test_x.py"]})
    package = TaskPackage(request=req, worktree="/wt/abc123", branch="agent-dispatch/abc123")
    rendered = render_prompt(package, "claude", settings())
    assert "/wt/abc123/src/client.py" in rendered
    assert "/wt/abc123/tests/test_x.py" in rendered
    # Оба пути остаются в промпте: cwd — worktree, repo — исходная копия.
    assert "cwd: /wt/abc123" in rendered
    assert "HEAD of /repo/project" in rendered


def test_render_prompt_worktree_mode_rewrites_relative_files_verbatim():
    req = request(cwd="/repo/project", files=["src/client.py"])
    package = TaskPackage(request=req, worktree="/wt/abc123")
    rendered = render_prompt(package, "claude", settings())
    assert "- src/client.py" in rendered
    assert "/wt/abc123/src/client.py" not in rendered


def test_render_prompt_worktree_mode_respects_path_boundary():
    req = request(
        cwd="/repo/project",
        task="Read /repo/project2/src/other.py and compare with /repo/project",
    )
    package = TaskPackage(request=req, worktree="/wt/abc123")
    rendered = render_prompt(package, "claude", settings())
    assert "/repo/project2/src/other.py" in rendered
    # Точный корень репо переписывается, соседний путь — нет.
    assert "compare with /wt/abc123" in rendered
    assert "/repo/project2/wt" not in rendered
    assert "/wt/abc1232" not in rendered


def test_render_prompt_worktree_mode_keeps_sibling_paths_with_punctuation():
    req = request(
        cwd="/repo/project",
        task="Compare /repo/project-old/a.py and /repo/project.bak with /repo/project/a.py",
    )
    package = TaskPackage(request=req, worktree="/wt/abc123")
    rendered = render_prompt(package, "claude", settings())
    assert "/repo/project-old/a.py" in rendered
    assert "/repo/project.bak" in rendered
    assert "with /wt/abc123/a.py" in rendered


def test_render_prompt_worktree_mode_maps_from_repo_root_for_subdir_cwd(git_repo):
    # Исполнитель стартует в корне worktree, даже если cwd запроса это подкаталог.
    sub = git_repo / "pkg"
    sub.mkdir()
    req = request(cwd=str(sub), task=f"Fix {git_repo}/pkg/mod.py")
    package = build_task_package(req, settings(), worktree="/wt/abc123")
    rendered = render_prompt(package, "claude", settings())
    assert "Fix /wt/abc123/pkg/mod.py" in rendered


def test_render_prompt_in_place_mode_keeps_paths_verbatim():
    req = request(
        cwd="/repo/project",
        task="Read /repo/project/src/client.py and fix the failing test",
    )
    rendered = render_prompt(TaskPackage(request=req), "claude", settings())
    assert "/repo/project/src/client.py" in rendered
    assert "/wt/" not in rendered
