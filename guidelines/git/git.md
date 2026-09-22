---
applies_when: Mutating or remote git/gh work — commit, push, PR create/merge, rebase, waiting on CI, switching worktrees. Read-only git (status/log/diff) is out of scope.
---

## Rules

- **Never hand-roll a wait loop for a long-running external job** — no `sleep` plus a re-poll inside a `for`/`while`. Use the tool's own blocking wait: `gh pr checks <pr> --watch`, `gh run watch`, or a `merge-when-green.sh` — the repo's own `scripts/merge-when-green.sh` when it exists, otherwise the repo-agnostic `~/.agents/guidelines/git/merge-when-green.sh` (works from any cwd; `--repo <owner>/<name>` needs no checkout). A foreground poll loop blocks the session for its full duration and produces nothing.
- **Stay in the worktree the session was opened in.** Acting on another worktree or the main checkout needs an explicit reason, stated to the user first.

## Verify

```bash
# No hand-rolled poll loops in what you just wrote or ran (expect no hits)
grep -nE '(for|while).*(sleep|gh (pr|run))' <file-or-script> || true

# You are still in the worktree the session opened in
git rev-parse --show-toplevel
```
