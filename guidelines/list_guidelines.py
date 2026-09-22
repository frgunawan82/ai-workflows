#!/usr/bin/env python3
"""List repo guidelines using file path and `applies_when` frontmatter.

Guidelines are not workflows: they answer "what rules bind me while I work",
several can apply at once, and whoever is acting (main session or subagent)
applies them inline — a guideline is never delegated.

Scans for Markdown files whose frontmatter contains `applies_when`, both
directly inside a guideline dir and one level deep (`*.md` and `*/*.md`):
- Global:        `~/.agents/guidelines/` (this repo)
- Project-local: the nearest `.agents/guidelines/` found by walking up from
  the current working directory (skipped when it resolves to the global dir).

A file without an `applies_when` value is not a discovery entry.

Project-over-global shadowing: when a project-local guideline *folder* name
equals a global guideline folder name, the project-local entries win and the
global folder's entries are not listed.

Outputs a Markdown table with 2 columns:
- Path
- Applies when

Display rules:
- Global guidelines -> path relative to `~/.agents` (e.g. `guidelines/...`)
- Project-local     -> path relative to the project root (e.g.
  `.agents/guidelines/...`)

Modes:
  (no args)  print the resolved table (global + nearest project-local)
  --sync     rewrite the generated block in ~/.pi/agent/AGENTS.md with the
             table of GLOBAL guidelines only (project-local ones are resolved
             at runtime by the bare run, never written into the global file)
  --check    exit 1 when that generated block is stale, 0 when in sync

Exit codes:
  0  success (or: block in sync for --check)
  1  no guideline files found / generated block is stale
  2  usage error
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

# Script lives at <repo>/guidelines/list_guidelines.py, so the repo root is
# one level up (the workflows scripts sit one level deeper and use parents[2]).
REPO_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_GUIDELINE_DIR = REPO_ROOT / "guidelines"

# Project-local guideline locations, searched by walking up from the cwd.
PROJECT_GUIDELINE_SUBDIRS = (Path(".agents") / "guidelines",)

AGENTS_MD = Path.home() / ".pi" / "agent" / "AGENTS.md"
BEGIN_MARKER = "<!-- BEGIN GENERATED -->"
END_MARKER = "<!-- END GENERATED -->"

FRONTMATTER_KEYS = ("applies_when",)


def find_project_guideline_dirs(start: Path) -> list[tuple[Path, Path]]:
    """Nearest project-local guideline dirs walking up from `start`.

    For each entry in PROJECT_GUIDELINE_SUBDIRS, return the nearest match as
    `(guideline_dir, project_root)`. The global dir is excluded so it is never
    reported twice.
    """
    global_resolved = GLOBAL_GUIDELINE_DIR.resolve()
    found: list[tuple[Path, Path]] = []
    for sub in PROJECT_GUIDELINE_SUBDIRS:
        for base in (start, *start.parents):
            candidate = base / sub
            if candidate.is_dir() and candidate.resolve() != global_resolved:
                found.append((candidate, base))
                break
    return found


def extract_frontmatter(path: Path) -> dict[str, str]:
    """Read known frontmatter keys. Empty dict when there is no frontmatter."""
    fields: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
            if first.startswith("\ufeff"):
                first = first.lstrip("\ufeff")

            if first.strip() != "---":
                return {}

            for line in handle:
                stripped = line.rstrip("\n")
                if stripped in {"---", "..."}:
                    break
                for key in FRONTMATTER_KEYS:
                    if stripped.startswith(f"{key}:"):
                        value = stripped.split(":", 1)[1].strip()
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                            value = value[1:-1]
                        fields[key] = value
                        break
    except OSError:
        return {}

    return fields


def escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|")


def collect(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    paths = set(dir_path.glob("*.md")) | set(dir_path.glob("*/*.md"))
    return sorted(p for p in paths if extract_frontmatter(p).get("applies_when"))


def guideline_folder(path: Path, base: Path) -> str | None:
    """Guideline folder name of an entry, or None for a file directly in `base`."""
    try:
        rel = path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def drop_shadowed(entries: list[Path], base: Path, shadow_names: set[str]) -> list[Path]:
    """Drop entries whose guideline folder name is claimed by a project-local folder."""
    return [p for p in entries if guideline_folder(p, base) not in shadow_names]


def display_path(path: Path, project_roots: list[Path]) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        pass
    for root in project_roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(resolved)


def render_table(entries: list[Path], project_roots: list[Path]) -> list[str]:
    lines = ["| Path | Applies when |", "| ---- | ------------ |"]
    for path in entries:
        rel = display_path(path, project_roots)
        applies_when = escape_markdown_cell(extract_frontmatter(path).get("applies_when", ""))
        lines.append(f"| `{rel}` | {applies_when} |")
    return lines


def resolve_entries() -> tuple[list[Path], list[Path]]:
    """Resolved guideline entries (global + nearest project-local) and project roots."""
    project_dirs = find_project_guideline_dirs(Path.cwd())
    project_roots = [root for _, root in project_dirs]

    project_entries: list[Path] = []
    shadow_names: set[str] = set()
    for guideline_dir, _ in project_dirs:
        found = collect(guideline_dir)
        project_entries += found
        shadow_names |= {
            name for p in found if (name := guideline_folder(p, guideline_dir)) is not None
        }

    entries = drop_shadowed(collect(GLOBAL_GUIDELINE_DIR), GLOBAL_GUIDELINE_DIR, shadow_names)
    entries += project_entries
    return entries, project_roots


def global_block() -> list[str]:
    """The generated block content for GLOBAL guidelines only."""
    return [BEGIN_MARKER, *render_table(collect(GLOBAL_GUIDELINE_DIR), []), END_MARKER]


def read_agents_md() -> list[str]:
    if not AGENTS_MD.is_file():
        print(f"error: {AGENTS_MD} not found", file=sys.stderr)
        raise SystemExit(2)
    return AGENTS_MD.read_text(encoding="utf-8").split("\n")


def locate_block(lines: list[str]) -> tuple[int, int]:
    try:
        begin = lines.index(BEGIN_MARKER)
        end = lines.index(END_MARKER)
    except ValueError:
        print(
            f"error: {AGENTS_MD} has no {BEGIN_MARKER} / {END_MARKER} block",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    if end < begin:
        print(f"error: {END_MARKER} precedes {BEGIN_MARKER} in {AGENTS_MD}", file=sys.stderr)
        raise SystemExit(2)
    return begin, end


def cmd_sync() -> int:
    lines = read_agents_md()
    begin, end = locate_block(lines)
    new_block = global_block()
    if lines[begin : end + 1] == new_block:
        print(f"already in sync: {AGENTS_MD}")
        return 0
    updated = lines[:begin] + new_block + lines[end + 1 :]
    AGENTS_MD.write_text("\n".join(updated), encoding="utf-8")
    # block = begin marker + header + separator + N rows + end marker
    print(f"synced {len(new_block) - 4} guideline(s) into {AGENTS_MD}")
    return 0


def cmd_check() -> int:
    lines = read_agents_md()
    begin, end = locate_block(lines)
    current = lines[begin : end + 1]
    expected = global_block()
    if current == expected:
        print(f"in sync: {AGENTS_MD}")
        return 0
    print(f"STALE: generated block in {AGENTS_MD} does not match global guidelines")
    diff = difflib.unified_diff(current, expected, fromfile="AGENTS.md", tofile="expected", lineterm="")
    print("\n".join(diff))
    return 1


def cmd_list() -> int:
    entries, project_roots = resolve_entries()
    if not entries:
        return 1
    print("\n".join(render_table(entries, project_roots)))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(f"usage: {Path(__file__).name} [--sync | --check]", file=sys.stderr)
        return 2
    if not argv:
        return cmd_list()
    if argv[0] == "--sync":
        return cmd_sync()
    if argv[0] == "--check":
        return cmd_check()
    if argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    print(f"usage: {Path(__file__).name} [--sync | --check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
