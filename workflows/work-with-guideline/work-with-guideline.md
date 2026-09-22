---
trigger: "Create a guideline", add/edit/rename/delete one, or convert to the standard — any file under guidelines/<folder>/. Authoring a guideline is a task, never inline.
---

## Trigger

"Create a guideline for X", "add / update / edit a guideline", "rename that guideline", "delete / remove a guideline", "convert this into a guideline" or "bring it into the guideline standard" — any change to files under a `guidelines/<folder>/`. Authoring, editing, renaming, or deleting a guideline is a task routed to this workflow, even though *applying* a guideline is done inline by the actor.

## Goal

The guideline folder reaches the requested state (created, edited, renamed, or deleted), conforms to the standard above, the generated block in `~/.pi/agent/AGENTS.md` matches the guidelines on disk, and every check in Verify passes.

## Context

**A guideline is not a workflow.** A workflow is a *task*: something to execute end to end, and the actor doing the work follows the matched workflow — the shared delegation sizing decides whether that actor is the main session or a subagent. A guideline is a *rule set*: it binds **how** work is done while some other task runs. Several guidelines can apply at once, and whoever is acting — main session or subagent — applies them inline. A guideline is never delegated and never spawns a subagent of its own. If the thing you are writing has ordered steps and an end state, it is a workflow: use `work-with-workflow`.

**The guideline standard.** Two body sections, nothing more.

1. Frontmatter — `applies_when` only (required, <=170 characters), nothing else. It is the routing filter: it must name the domain and, where useful, what is deliberately out of scope.
2. `## Rules` — required. A bullet list of imperative rules, strongest first, each stating the rule and the reason it exists. Bullets, never numbered steps — numbering implies an execution order, which is workflow shape.
3. `## Verify` — optional, and last when present. Exact commands in a fenced block that prove the rules were honoured. Many rule sets cannot be checked by a command; when that is the case, omit the section rather than inventing a hollow check.

**Why `## Applies when` was removed.** The old format repeated the frontmatter key as a body section. Two copies of one fact drift apart, and the body copy was never read by anything: discovery reads the frontmatter, and the model reading the file already has the frontmatter in front of it. The frontmatter key is the single source of truth.

**Discovery and routing.** `guidelines/list_guidelines.py` lists `.md` files carrying an `applies_when` frontmatter key (`*.md` and `*/*.md`) from the global `~/.agents/guidelines/` plus the nearest project-local `.agents/guidelines/` walking up from the cwd. `--sync` rewrites the generated block between `<!-- BEGIN GENERATED -->` and `<!-- END GENERATED -->` in `~/.pi/agent/AGENTS.md` with the **global** table only; `--check` exits 1 when that block is stale. That block is the zero-cost filter every session reads at startup, so a guideline that is not in it effectively does not exist. Project-local guidelines are deliberately never written into it — they resolve at runtime.

**Folder-name shadowing.** Each guideline owns `guidelines/<folder>/`, and the *folder name* is the shadowing unit: a project-local `.agents/guidelines/<same-folder-name>/` replaces the global folder wholesale. Name the folder after the domain (`git`, not `git-rules-v2`), because a project overriding your guideline has to guess that name.

**Validation.** `guidelines/check_guideline.py` enforces this standard deterministically — frontmatter shape, allowed sections and their order, bullets-not-numbered-steps, fenced Verify, size, and the `guidelines/<folder>/<name>.md` location. Run it instead of eyeballing the format. Its sibling `workflows/work-with-workflow/check_workflow.py` does the same for workflows.

**Rename lesson.** Renaming a guideline folder silently changes its shadowing identity and breaks every path that referenced it — after a rename, re-run `--sync` and grep for the old folder name across workflows, scripts, and AGENTS.md.

## Constraints

- Every guideline you create or edit ends up in the standard above: frontmatter `applies_when` only, `## Rules` required, `## Verify` optional and last. Never add an `## Applies when` section or any other `##` heading.
- Keep rules as `- ` bullets. Numbered steps are workflow shape — if the content needs them, it belongs in a workflow, not a guideline.
- Run `list_guidelines.py --sync` after any create, rename, or delete, and after any change to an `applies_when` value. A stale routing table is the same as a missing guideline.
- Never hand-edit the generated block in `~/.pi/agent/AGENTS.md`; it is owned by `--sync`. Edit the guideline file and re-sync.
- Delete a guideline only on explicit user request, and re-sync immediately afterwards.
- Prefer deterministic checks. Decision order per step: Python script → one-line shell → LLM step. Run `check_guideline.py` rather than reviewing the format by hand; every surviving LLM step carries a one-line `LLM needed because <X>` rationale.
- Once an LLM step is genuinely needed, delegate research and verbose scans to a subagent; keep small edits inline. In-session delegation uses the hosting harness's own subagent tool — name it generically, never a harness-specific tool name or CLI. Any headless or programmatic model call goes through `$HOME/.agents/workflows/casper/LLM_harness.sh`.
- Never create guideline-local `.venv` or `node_modules`; Python runs via `$HOME/.agents/.venv/bin/python`.
- Create gate: ask clarifying questions and do not start building until the domain, the `applies_when` filter, and the rules themselves are clear. If the answer describes a task with steps, stop and route to `work-with-workflow`.
- Guideline file stays under 8000 characters. Do not register guidelines in AGENTS.md by hand — `--sync` does it.

## Verify

```bash
PY="$HOME/.agents/.venv/bin/python"

# The guideline conforms to the standard (exit 0, no violations)
"$PY" "$HOME/.agents/guidelines/check_guideline.py" "$HOME/.agents/guidelines/<folder>/<name>.md"

# Every global guideline still conforms
"$PY" "$HOME/.agents/guidelines/check_guideline.py"

# The generated block in ~/.pi/agent/AGENTS.md is current (run --sync first if not)
"$PY" "$HOME/.agents/guidelines/list_guidelines.py" --check

# Discovery reflects the change (created/edited appears; deleted is gone)
"$PY" "$HOME/.agents/guidelines/list_guidelines.py"

# No scattered deps under guidelines/ (shared venv lives in $HOME/.agents)
find "$HOME/.agents/guidelines" -type d \( -name .venv -o -name node_modules \) -print
```
