#!/usr/bin/env python3
"""Validate guideline files against the guideline standard.

A guideline is a LIST OF RULES that bind *how* work is done. Its standard is
deliberately smaller than the workflow five-field standard:

  frontmatter : `applies_when` only (required, <=170 characters), nothing else
  `## Rules`  : required, a bullet list of imperative rules
  `## Verify` : optional; when present it holds exact commands in a fenced
                code block

`## Applies when` is NOT a body section: it duplicated the frontmatter key and
was removed from the standard.

Blocking violations (exit 1), each printed as `path:line: message`:
  - missing or invalid frontmatter
  - missing `applies_when`
  - a frontmatter key other than `applies_when`
  - `applies_when` longer than 170 characters
  - an `## Applies when` section (the removed field)
  - any `##` heading outside {Rules, Verify}
  - missing `## Rules`
  - `## Verify` placed before `## Rules` when both are present
  - `## Rules` body with no `- ` bullet
  - `## Rules` body with numbered step prose (`1.` / `2.` line starts) —
    that is workflow shape, not guideline shape
  - `## Verify` present but with no fenced code block
  - file longer than 8000 characters
  - file not one level deep inside a guidelines dir
    (`guidelines/<folder>/<name>.md`; the folder name is the
    project-over-global shadowing unit)

Usage:
  check_guideline.py [PATH ...]

PATH may be a guideline file or a directory (searched recursively for `.md`).
With no PATH, every global guideline under `~/.agents/guidelines/` is checked.

Exit codes:
  0  all checked files conform
  1  violations found
  2  usage error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_GUIDELINE_DIR = REPO_ROOT / "guidelines"

MAX_APPLIES_WHEN = 170
MAX_CHARS = 8000
ALLOWED_FRONTMATTER = {"applies_when"}
ALLOWED_SECTIONS = ("Rules", "Verify")

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
BULLET_RE = re.compile(r"^\s*[-*]\s+\S")
NUMBERED_RE = re.compile(r"^\s*\d+\.\s+\S")
FENCE_RE = re.compile(r"^\s*```")


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


def check_file(path: Path) -> list[str]:
    problems: list[str] = []

    def add(line: int, message: str) -> None:
        problems.append(f"{path}:{line}: {message}")

    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # Location: guidelines/<folder>/<name>.md
    resolved = path.resolve()
    if resolved.parent.parent.name != "guidelines" or resolved.parent.name == "guidelines":
        add(1, "file is not one level deep inside a guidelines dir (expected guidelines/<folder>/<name>.md)")

    if len(text) > MAX_CHARS:
        add(1, f"file is {len(text)} characters, over the {MAX_CHARS}-character limit")

    fields, fm_end = parse_frontmatter(lines)
    if fm_end is None:
        add(1, "missing or invalid frontmatter (expected a leading `---` block closed by `---`)")
    else:
        for key, (value, lineno) in fields.items():
            if key not in ALLOWED_FRONTMATTER:
                add(lineno, f"unexpected frontmatter key `{key}` (only `applies_when` is allowed)")
        if "applies_when" not in fields:
            add(1, "frontmatter is missing the required `applies_when` key")
        else:
            value, lineno = fields["applies_when"]
            if not value:
                add(lineno, "`applies_when` is empty")
            elif len(value) > MAX_APPLIES_WHEN:
                add(lineno, f"`applies_when` is {len(value)} characters, over the {MAX_APPLIES_WHEN}-character limit")

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

    for name, lineno in headings:
        if name.strip().lower() == "applies when":
            add(lineno, "`## Applies when` is not part of the guideline standard — the frontmatter `applies_when` already carries it; delete the section")
        elif name not in ALLOWED_SECTIONS:
            add(lineno, f"unexpected `##` heading `{name}` (only `## Rules` and `## Verify` are allowed)")

    names = [name for name, _ in headings]
    if "Rules" not in names:
        add(1, "missing the required `## Rules` section")
    if "Verify" in names and "Rules" in names and names.index("Verify") < names.index("Rules"):
        add(headings[names.index("Verify")][1], "`## Verify` appears before `## Rules`; `## Rules` comes first")

    def section_body(target: str) -> tuple[list[str], int] | None:
        for position, (name, lineno) in enumerate(headings):
            if name != target:
                continue
            end = headings[position + 1][1] - 1 if position + 1 < len(headings) else len(lines)
            return lines[lineno:end], lineno
        return None

    rules = section_body("Rules")
    if rules is not None:
        body, lineno = rules
        if not any(BULLET_RE.match(line) for line in body):
            add(lineno, "`## Rules` contains no `- ` bullet; a guideline is a bullet list of rules")
        for offset, line in enumerate(body):
            if NUMBERED_RE.match(line):
                add(lineno + offset + 1, "numbered step prose in `## Rules` — numbered steps are workflow shape; write rules as `- ` bullets")
                break

    verify = section_body("Verify")
    if verify is not None:
        body, lineno = verify
        if not any(FENCE_RE.match(line) for line in body):
            add(lineno, "`## Verify` has no fenced code block; verification must be exact commands in a ``` fence")

    return problems


def collect(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.rglob("*.md") if p.is_file())
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
    parser.add_argument("paths", nargs="*", help="guideline files or directories (default: all global guidelines)")
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.paths] or [GLOBAL_GUIDELINE_DIR]
    for target in targets:
        if not target.exists():
            print(f"error: no such file or directory: {target}", file=sys.stderr)
            return 2

    files = collect(targets)
    if not files:
        print("checked 0 guideline(s): 0 violation(s)")
        return 0

    violations = 0
    bad_files = 0
    for path in files:
        try:
            problems = check_file(path)
        except OSError as exc:
            print(f"{path}:1: cannot read file ({exc})")
            problems = ["unreadable"]
        for problem in problems:
            print(problem)
        if problems:
            bad_files += 1
            violations += len(problems)

    print(f"checked {len(files)} guideline(s): {violations} violation(s) in {bad_files} file(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
