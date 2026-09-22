#!/usr/bin/env python3
"""Validate workflow files against the five-field standard.

Every workflow file is frontmatter plus exactly five `##` sections:

  frontmatter : `trigger` (required, <=170 characters) plus optionally
                `model` and `effort`; nothing else
  sections    : Trigger, Goal, Context, Constraints, Verify — in that order

Blocking violations (exit 1), each printed as `path:line: message`:
  - a frontmatter key outside {trigger, model, effort}
  - missing `trigger`
  - `trigger` longer than 170 characters
  - the `##` sections are not exactly Trigger, Goal, Context, Constraints,
    Verify in that order
  - file longer than 8000 characters
  - a direct provider-CLI agent call anywhere in the workflow's folder
    (`claude -p|--print`, `pi -p|--print`) — those must go through
    `workflows/casper/LLM_harness.sh`
  - a `.venv` or `node_modules` directory inside the workflow folder

Non-blocking warning (printed as `path:line: WARNING ...`, does not affect the
exit code):
  - the file mentions LLM or subagent steps but carries no
    `LLM needed because` rationale

Usage:
  check_workflow.py [PATH ...]

PATH may be a workflow file or a directory (searched for discoverable `.md`
files, i.e. those carrying a `trigger` frontmatter key). With no PATH, every
global workflow under `~/.agents/workflows/` is checked.

Exit codes:
  0  all checked files conform (warnings may still be printed)
  1  violations found
  2  usage error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_WORKFLOW_DIR = REPO_ROOT / "workflows"

MAX_TRIGGER = 170
MAX_CHARS = 8000
ALLOWED_FRONTMATTER = ("trigger", "model", "effort")
REQUIRED_SECTIONS = ["Trigger", "Goal", "Context", "Constraints", "Verify"]
STRAY_DEP_DIRS = (".venv", "node_modules")

# The harness folder is exempt from the provider-CLI rule: `LLM_harness.sh`, its
# guards, and its docs ARE the sanctioned wrapper, so they necessarily spell out
# `claude -p` / `pi -p`. Every other workflow must route through them.
HARNESS_DIR = GLOBAL_WORKFLOW_DIR / "casper"

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*```")
PROVIDER_CLI_RE = re.compile(r"(?:^|[^_/\w])(?:claude|pi)[ \t]+(?:-p|--print)(?:[ \t]|$)")
LLM_MENTION_RE = re.compile(r"subagent|sub-agent|\bLLM\b", re.IGNORECASE)
RATIONALE_RE = re.compile(r"LLM needed because", re.IGNORECASE)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_frontmatter(lines: list[str]) -> tuple[dict[str, tuple[str, int]], int | None]:
    """Return ({key: (value, lineno)}, end_lineno) or ({}, None) when absent."""
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return {}, None
    fields: dict[str, tuple[str, int]] = {}
    for index in range(1, len(lines)):
        stripped = lines[index].rstrip()
        if stripped in {"---", "..."}:
            return fields, index + 1
        match = KEY_RE.match(stripped)
        if match:
            fields[match.group(1)] = (unquote(match.group(2)), index + 1)
    return fields, None


def workflow_folder(path: Path) -> Path:
    """The `workflows/<folder>/` dir owning `path` (fallback: the file's dir)."""
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.parent.name == "workflows":
            return parent
    return resolved.parent


def scan_folder(folder: Path) -> list[str]:
    """Folder-level violations: provider-CLI calls and stray dependency dirs."""
    problems: list[str] = []
    if not folder.is_dir():
        return problems

    for name in STRAY_DEP_DIRS:
        for stray in sorted(folder.rglob(name)):
            if stray.is_dir():
                problems.append(f"{stray}:1: stray `{name}` directory inside the workflow folder; use the shared runtimes in $HOME/.agents")

    if folder.resolve() == HARNESS_DIR.resolve():
        return problems

    for candidate in sorted(folder.rglob("*")):
        if not candidate.is_file():
            continue
        if any(part in STRAY_DEP_DIRS for part in candidate.parts):
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.split("\n"), start=1):
            if PROVIDER_CLI_RE.search(line):
                problems.append(f"{candidate}:{lineno}: direct provider-CLI agent call; route headless model calls through workflows/casper/LLM_harness.sh")
    return problems


def check_file(path: Path, folder_cache: dict[Path, list[str]]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    def add(line: int, message: str) -> None:
        problems.append(f"{path}:{line}: {message}")

    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    if len(text) > MAX_CHARS:
        add(1, f"file is {len(text)} characters, over the {MAX_CHARS}-character limit")

    fields, fm_end = parse_frontmatter(lines)
    if fm_end is None:
        add(1, "missing or invalid frontmatter (expected a leading `---` block closed by `---`)")
    else:
        for key, (value, lineno) in fields.items():
            if key not in ALLOWED_FRONTMATTER:
                add(lineno, f"unexpected frontmatter key `{key}` (only trigger, model, effort are allowed)")
        if "trigger" not in fields:
            add(1, "frontmatter is missing the required `trigger` key")
        else:
            value, lineno = fields["trigger"]
            if not value:
                add(lineno, "`trigger` is empty")
            elif len(value) > MAX_TRIGGER:
                add(lineno, f"`trigger` is {len(value)} characters, over the {MAX_TRIGGER}-character limit")

    body_start = fm_end or 0
    headings: list[tuple[str, int]] = []
    in_fence = False
    for index in range(body_start, len(lines)):
        if FENCE_RE.match(lines[index]):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(lines[index])
        if match:
            headings.append((match.group(1), index + 1))

    names = [name for name, _ in headings]
    if names != REQUIRED_SECTIONS:
        lineno = headings[0][1] if headings else 1
        add(lineno, "`##` sections are " + (str(names) if names else "[]") + f", expected exactly {REQUIRED_SECTIONS} in that order")

    folder = workflow_folder(path)
    if folder not in folder_cache:
        folder_cache[folder] = scan_folder(folder)
    problems += folder_cache[folder]

    if not RATIONALE_RE.search(text):
        for lineno, line in enumerate(lines, start=1):
            if LLM_MENTION_RE.search(line):
                warnings.append(f"{path}:{lineno}: WARNING mentions LLM/subagent steps but carries no `LLM needed because <X>` rationale")
                break

    return problems, warnings


def has_trigger(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return False
    fields, _ = parse_frontmatter(lines)
    return bool(fields.get("trigger"))


def collect(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = set(path.glob("*.md")) | set(path.glob("*/*.md")) | set(path.glob("*/*/*.md"))
            found += sorted(p for p in candidates if p.is_file() and has_trigger(p))
        else:
            found.append(path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", help="workflow files or directories (default: all global workflows)")
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.paths] or [GLOBAL_WORKFLOW_DIR]
    for target in targets:
        if not target.exists():
            print(f"error: no such file or directory: {target}", file=sys.stderr)
            return 2

    files = collect(targets)
    if not files:
        print("checked 0 workflow(s): 0 violation(s), 0 warning(s)")
        return 0

    folder_cache: dict[Path, list[str]] = {}
    violations = 0
    warned = 0
    bad_files = 0
    for path in files:
        try:
            problems, warnings = check_file(path, folder_cache)
        except OSError as exc:
            print(f"{path}:1: cannot read file ({exc})")
            problems, warnings = ["unreadable"], []
        for problem in problems:
            print(problem)
        for warning in warnings:
            print(warning)
        if problems:
            bad_files += 1
            violations += len(problems)
        warned += len(warnings)

    print(f"checked {len(files)} workflow(s): {violations} violation(s) in {bad_files} file(s), {warned} warning(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
