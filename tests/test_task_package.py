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
