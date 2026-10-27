"""Keep the per-agent adapters consistent with the shared project contract."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from research_project.agent_events import FILE_TOOLS, PATCH_TOOL, SHELL_TOOL


PROJECT = Path(__file__).resolve().parents[4]
HOOK_CONFIGS = {
    "codex": PROJECT / ".codex/hooks.example.json",
    "claude": PROJECT / ".claude/settings.example.json",
}
EDIT_TOOLS = {"codex": {PATCH_TOOL}, "claude": FILE_TOOLS - {"NotebookEdit"}}
KNOWN_TOOLS = FILE_TOOLS | {PATCH_TOOL, SHELL_TOOL, "Read"}
SHELL_TASKS = {"hook-pre-shell"}
# Codex manages its own --worktree sessions and has no equivalent hook events.
CLAUDE_ONLY_EVENTS = {"WorktreeCreate", "WorktreeRemove"}
TASK = re.compile(r"^pixi run --locked --manifest-path \S+ ([a-z-]+)$")

pytestmark = pytest.mark.skipif(
    not (PROJECT / "AGENTS.md").is_file(), reason="Project adapters are not present"
)


def hook_tasks(name: str) -> dict[str, list[tuple[str, str]]]:
    config = json.loads(HOOK_CONFIGS[name].read_text())
    tasks: dict[str, list[tuple[str, str]]] = {}
    for event, groups in config["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                match = TASK.match(hook["command"])
                assert match, f"{name} {event} hook must call a Pixi task"
                tasks.setdefault(event, []).append((group.get("matcher", ""), match[1]))
    return tasks


def test_claude_loads_the_shared_instructions_and_skills() -> None:
    lines = (PROJECT / "CLAUDE.md").read_text().splitlines()

    assert lines[0] == "@AGENTS.md"
    assert (PROJECT / ".claude/skills").resolve() == (PROJECT / ".agents/skills")


def test_shared_skills_have_portable_metadata() -> None:
    skills = sorted((PROJECT / ".agents/skills").iterdir())

    assert skills
    for skill in skills:
        text = (skill / "SKILL.md").read_text()
        header = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        assert header, f"{skill.name} needs YAML frontmatter"
        fields = dict(
            line.split(": ", 1) for line in header[1].splitlines() if ": " in line
        )
        assert fields.get("name") == skill.name
        assert fields.get("description")


def test_agents_run_the_same_hook_tasks() -> None:
    pixi = tomllib.loads((PROJECT / "code/pixi.toml").read_text())
    codex, claude = hook_tasks("codex"), hook_tasks("claude")

    assert set(codex) == set(claude) - CLAUDE_ONLY_EVENTS
    assert set(codex) == {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}
    for event in codex:
        assert [task for _, task in codex[event]] == [task for _, task in claude[event]]
    for event in claude:
        for _, task in claude[event]:
            assert task in pixi["tasks"]


@pytest.mark.parametrize("name", sorted(HOOK_CONFIGS))
def test_tool_hooks_match_each_agents_tools(name: str) -> None:
    for event in ("PreToolUse", "PostToolUse"):
        for matcher, task in hook_tasks(name)[event]:
            expected = {SHELL_TOOL} if task in SHELL_TASKS else EDIT_TOOLS[name]
            matched = {tool for tool in KNOWN_TOOLS if re.search(matcher, tool)}
            assert matched - {"NotebookEdit"} == expected, (name, task)
