# ai-workflows

A library of reusable, file-based workflows that live at `$HOME/.agents/workflows`
and work on **any LLM harness**.

## Why this exists

AI capability today gets locked inside whichever tool you happen to use. Your prompts,
your procedures, your hard-won "this is how we actually do it here" knowledge end up
as that vendor's config format — and moving harness means rebuilding all of it.

This project keeps that knowledge in plain Markdown files on your own disk instead.

**Harness-agnostic by construction.** A workflow is just a Markdown file describing
**when** it applies (a trigger), **what** end state to reach, and **how** to verify it
got there — plus any helper scripts. Nothing in that contract is vendor-specific. Any
agent that can read a file and run a shell command can follow it. Switch from one CLI
to another and your entire workflow library comes with you, unchanged.

**Cheap on the context window.** This is the part that makes it scale. Workflows are
not loaded into your prompt. The agent runs one script that prints a compact table of
workflow paths and triggers, matches the task against a trigger, and reads *only that
one file*. So a hundred workflows cost roughly what ten cost: one table, plus the
single file that actually applies. Adding a workflow does not tax every unrelated
conversation, and no per-tool context budget caps how much you can accumulate.

**Extend it freely.** Add as many workflows as you need — your own team conventions,
your deploy ritual, your review checklist. Drop a folder in `~/.agents/workflows/`,
or commit one to a project's `.agents/workflows/` where it shadows the global version
for that repo only. `work-with-workflow` is the workflow for writing workflows, and
`work-with-guideline` is its counterpart for [guidelines](#guidelines) — the rule sets
that bind *how* any task is done.

The result: capability that compounds on your disk rather than inside someone else's
product, and travels with you to whatever model or harness comes next.

## Install

```bash
git clone git@github.com:frgunawan82/ai-workflows.git
cd ai-workflows
./scripts/install.sh
```

That copies:

| From | To |
| ---- | -- |
| `workflows/` | `~/.agents/workflows/` |
| `guidelines/` | `~/.agents/guidelines/` |
| `AGENTS.md` | `~/.pi/agent/AGENTS.md`, `~/.codex/AGENTS.md`, `~/.opencode/AGENTS.md`, `~/.claude/CLAUDE.md` |
| `.env.example` | `~/.agents/.env` (only if absent, `chmod 600`) |
| `.config.example` | `~/.agents/.config` (only if absent) |

The four agent-instruction targets are the same file — each harness looks in a
different place, so the installer writes it to all four.

Run `./scripts/install.sh --dry-run` first to see exactly what it would do. It
is safe to re-run: anything it would overwrite is moved to `<file>.bak.<stamp>`
first, and once `~/.agents/.env` and `~/.agents/.config` exist it never touches
them again, so re-running to pick up new workflows will not clobber your
credentials. Use `--home DIR` to install somewhere other than `$HOME`.

Workflow commands reference `$HOME/.agents/workflows/...`, which is why the
install location is fixed. Several also call a shared Python environment at
`$HOME/.agents/.venv`:

```bash
python3 -m venv ~/.agents/.venv
```

Then install per-workflow requirements as needed (for example
`~/.agents/.venv/bin/pip install -r ~/.agents/workflows/playwright/requirements.txt`).

## Listing the workflows

```bash
$HOME/.agents/.venv/bin/python $HOME/.agents/workflows/work-with-workflow/list_workflows.py
```

This prints every workflow's path and trigger, from the global `~/.agents/workflows/`
plus the nearest project-local `.agents/workflows/`. On a folder-name collision the
project-local workflow shadows the global one. Exit `1` means none were found.

Point your agent at that script rather than hardcoding a list — that indirection is
what keeps the context cost flat as the library grows. [`AGENTS.md`](AGENTS.md) is the
agent-facing instruction file that wires this up; copy it into your harness's own
instruction file (`AGENTS.md`, `CLAUDE.md`, or whatever yours reads) and adjust.

## Guidelines

A **workflow** is a task: ordered steps and an end state. A **guideline** is a rule
set: it binds *how* work is done while some other task runs. Several can apply at once,
and whoever is acting — main session or subagent — applies them inline. A guideline is
never delegated and never spawns a subagent of its own.

A guideline is one Markdown file at `~/.agents/guidelines/<folder>/<name>.md` (or a
project's `.agents/guidelines/`, which shadows the global folder of the same name). Its
frontmatter has a single key, `applies_when`, followed by a `## Rules` bullet list and
an optional `## Verify` block. Discovery mirrors the workflow scheme:

```bash
PY=$HOME/.agents/.venv/bin/python
$PY $HOME/.agents/guidelines/list_guidelines.py          # resolved table, global + project-local
$PY $HOME/.agents/guidelines/list_guidelines.py --sync   # rewrite the routing block in ~/.pi/agent/AGENTS.md
$PY $HOME/.agents/guidelines/list_guidelines.py --check  # exit 1 if that block is stale
```

`--sync` rewrites the block between `<!-- BEGIN GENERATED -->` and
`<!-- END GENERATED -->` in `AGENTS.md` with the table of global guidelines, so every
session sees the routing filter at zero lookup cost. The shipped `AGENTS.md` already
carries the block for the guidelines shipped here; it is owned by `--sync` and never
hand-edited. `check_guideline.py` validates a guideline file against the standard the
same way `work-with-workflow/check_workflow.py` validates a workflow; CI runs both.
`work-with-guideline` is the workflow for creating, editing, renaming, or deleting a
guideline.

## What's here

### Engineering workflows

| Workflow | Use it when |
| --- | --- |
| `dead-code-cleanup` | Find and prune unused production code, exports, deps. |
| `dead-test-cleanup` | Find and prune orphan or vacuous tests. |
| `specs-optimization` | Analyze how a repo's specs are split for agent consumption. |
| `work-with-workflow` | Create, edit, or delete a workflow in this format. |
| `work-with-guideline` | Create, edit, rename, or delete a guideline in the standard format. |

There is deliberately no `implement`, `planning`, `review`, or `bug-fix` workflow.
Generic reasoning work carries no information the model lacks, so it is done directly
under `AGENTS.md`; workflows exist for project-, tool-, or domain-specific procedure.

### Agent runtime

| Workflow | Use it when |
| --- | --- |
| `casper` | Run an approved complex goal as resumable, claim-safe fan-out execution. |

### Guidelines

| Guideline | Applies when |
| --- | --- |
| `git` | Mutating or remote git/gh work. Ships `merge-when-green.sh`: wait for every PR check, merge only if all are green, never bypass. |
| `playwright` | Any Playwright session: headed vs headless, and loading/saving the `.auth/` profile. |

### Browser automation

| Workflow | Use it when |
| --- | --- |
| `playwright` | Open, read, scrape, or debug a site with a local browser. |
| `kernel-browser` | Same, on a hosted cloud browser (requires a kernel.sh account). |
| `notebooklm-workflow` | Drive Google NotebookLM through browser automation. |

### Google Workspace & SaaS integrations

| Workflow | Use it when |
| --- | --- |
| `google-auth-workflow` | Verify Google OAuth across the Docs/Sheets/Slides workflows. |
| `gmail-workflow` | Read, classify, and compose Gmail via the official API. |
| `gdocs-workflow` | Create and edit Google Docs. |
| `gsheet-workflow` | Read and write Google Sheets. |
| `gslides-workflow` | Build Google Slides decks. |
| `clickup-workflow` | Create and update ClickUp tasks. |

## Credentials

**No credentials, tokens, cookies, browser profiles, or exported data are in this
repository, and none should ever be committed to it.** `.gitignore` is written to
block them; do not weaken it.

You provide your own credentials. There are two files, both outside the repository in
`$HOME/.agents/`, and both gitignored:

| File | Holds | Template |
| ---- | ----- | -------- |
| `~/.agents/.env` | Secrets — API tokens and webhook URLs. | [`.env.example`](.env.example) |
| `~/.agents/.config` | Non-secret settings — workspace IDs, rate limits, timezones. | [`.config.example`](.config.example) |

Copy each template and fill in what the workflows you use actually need:

```sh
cp .env.example    ~/.agents/.env    && chmod 600 ~/.agents/.env
cp .config.example ~/.agents/.config
```

Every variable is listed in the templates with a comment saying which workflow reads it
and where to obtain the value. Nothing is required up front — a workflow fails with a
named variable when something it needs is missing. A real environment variable always
overrides both files, so you can export one for a single run or in CI.

**Credentials that cannot be environment variables** stay as files, because the client
libraries rewrite them: Google OAuth client secrets and their refreshable token caches,
and saved browser session state. These live in one gitignored directory whose path is
set by `GOOGLE_CREDENTIALS_DIR` in `.env`. Browser session state is written under the
owning workflow's `.auth/`, also gitignored.

## Prerequisites

These are per-workflow, not global — read the workflow file before running it.

- **Python 3** at `$HOME/.agents/.venv` for most scripted workflows.
- **`claude` and/or `pi` CLI** on `PATH` for `casper` and `specs-optimization`. The model
  alias table in `casper/LLM_harness.sh` is meant to be edited to match whatever your
  CLI accepts.
- **Node + `npx`** for `dead-code-cleanup` (uses `knip`).
- **Node + `npm`** for `kernel-browser` and TypeScript helpers:
  `npm install --prefix ~/.agents tsx @onkernel/sdk`.
- **Playwright browsers** for `playwright`:
  `PLAYWRIGHT_BROWSERS_PATH=workflows/playwright/.browsers python -m playwright install chromium`.
  Browser binaries are never committed — see `THIRD-PARTY-NOTICES.md` for why.
- **A kernel.sh account** and `KERNEL_API_KEY` for `kernel-browser`.
- **`gh` CLI** for the `git` guideline's `merge-when-green.sh`.

## ⚠️ Terms-of-Service notice

Some workflows in this repository automate a web browser against third-party services
(including Google and other SaaS products) using a session that you log into yourself.
These workflows are published as a personal-automation reference and are intended
solely for accessing **your own accounts and your own data**, with your own consent.

Automated access may violate the terms of service of the services involved, and those
terms can change at any time. It is **your** responsibility, before running anything
here, to read the applicable terms, confirm you are entitled to access the data in
question, and obtain consent from anyone else whose data may appear in it. Do not use
these workflows to access accounts you do not own, to scrape or redistribute
third-party content, to circumvent rate limits, access controls, or bot-detection
measures, or to process personal data without a lawful basis.

No credentials, session state, cookies, browser profiles, or exported data are included
in this repository, and none should ever be committed to it.

This software is provided "as is", without warranty of any kind. The authors accept no
liability for account suspension, data loss, contractual breach, or any other
consequence of its use.

## License

MIT — see [LICENSE](LICENSE). Third-party dependency notes are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
