---
trigger: "Run casper on this", "execute this goal", "continue the handover" — an approved complex goal needing resumable, claim-safe execution; also any existing handover.
---

## Trigger

"Run casper on this", "execute this goal", "continue the handover", or an existing casper run under `~/.agents/state/casper/<slug>/`.

## Goal

The approved `$HD/goal.md` is completed, every acceptance criterion passes verification, the result is reported, and the transient `$HD` is cleaned up.

## Context

**Pipeline.** `casper_fanout.py` claims open plans, one `LLM_harness.sh` executor call each; `casper_verify.py` decides acceptance criteria; `casper_cleanup.py` retires `$HD`; `casper_status.py` owns the `plans.json` status/lease ledger (plan frontmatter mirrors it). `casper_guard.py` (Claude token guard) and `casper_pi_guard.py` (Pi rpc guard) wrap dispatches; details, warm resume, zero-progress stops: [`models-and-guards.md`](models-and-guards.md). LLM needed because open-ended engineering work and subjective acceptance criteria cannot be enumerated in code.

**Setup.**

```bash
PY="$HOME/.agents/.venv/bin/python"
WF="$HOME/.agents/workflows/casper"
SLUG="<kebab-case-goal>"
HD="$HOME/.agents/state/casper/$SLUG"
mkdir -p "$HD/logs"
```

**Contracts.** `$HD/goal.md` is the execution contract; its shape and pairing rules, plan shapes, checklist and split/wave rules: [`templates.md`](templates.md).

**Plan ledger.**

```bash
"$PY" "$WF/casper_status.py" --scaffold --handover-dir "$HD"  # plan-01-$SLUG.md + plans.json seeded; exit 1 = no goal.md
```

Fill the scaffolded plan's `## Objective`. For split plans (see Constraints) write the plan files, list every plan (not only new ones; `plans.json` entry shape) in `$HD/manifest.json`, then `"$PY" "$WF/casper_status.py" --init --from-manifest "$HD/manifest.json" --handover-dir "$HD"` — the ledger becomes exactly that list; known plans keep their status.

**Detached fanout + watcher.** A fanout run outlives any single tool call: launch it detached, then watch it so completion wakes the driver:

```bash
setsid nohup "$PY" "$WF/casper_fanout.py" --handover-dir "$HD" >> "$HD/logs/fanout.log" 2>&1 & disown
sleep 1; pgrep -f "^[^ ]*python[^ ]* [^ ]*casper_fanout.py --handover-dir $HD"  # PID; the ^…python anchor excludes the calling shell
```

Empty output means fanout already exited (nothing open, or every open plan carries a `NEEDS-USER:`) — read `fanout-result.json`, no watcher. Otherwise, in a background shell call whose completion wakes the driver (a crash kills only the watcher; the resume checklist recovers it):

```bash
while kill -0 <PID> 2>/dev/null; do sleep 30; done
tail -n 3 "$HD/logs/fanout.log"; cat "$HD/fanout-result.json"
```

Paste the PID literally — shell state does not persist between calls. Fanout appends per-plan output to `logs/<plan>.log` and atomically rewrites `fanout-result.json` with the latest outcomes (incl. `skipped_host_busy`); override `--model`/`--stopwatch`/`--max-context-tokens`/`--min-free-gb`/`--host-wait` as needed; re-runs are idempotent — done plans skipped, paused/failed retried, live claims never duplicated.

**Completion semantics.** A resolver must explicitly mark its plan done via `casper_status.py`; exit `0` without that signal pauses it. Timeout and token-budget exits (`124`/`137`) pause; other nonzero exits fail. A done signal survives a later timeout or nonzero exit — but an open `NEEDS-USER:` always wins and pauses the plan.

**Checkpoint and escalation.** `## Progress / Handover` is a bounded replacement checkpoint, never an append-only journal — fanout embeds that contract and wind-down obedience in every resolver prompt and compacts it to 4000 characters (logs retain detail). An executor facing an irreversible/destructive or underivable decision records exactly one open `NEEDS-USER: <question and options>` line, finishes independent safe work, and stops. A `failed` plan's next attempt runs one level above its configured/default effort (`medium`→`high`; `max` stays), not cumulative; a pause never escalates.

**Lease safety.** Claims are atomic under a file lock and last stopwatch + grace + slack seconds (default 7800), recorded in the lease; `--stale-secs` lengthens but never shortens a recorded claim, even at `0` — wait for expiry. `--list-open` prints dispatchable work, not live-leased plans.

**Verification semantics.**

```bash
"$PY" "$WF/casper_verify.py" --handover-dir "$HD" --cwd "<project-root>"
```

The verifier refuses while any plan is unresolved (incl. live `in_progress`), decides each `command` method only from its declared timeout (default 300 s) and exit status, batches all `judgment` methods into one harness call (none without them), and never fixes work. Every run atomically replaces `$HD/verify.json`, even on contract or execution failure — stale passing evidence never authorizes cleanup. On a failed criterion: map the evidence to its plan, `--set <plan> failed` it, replace its checkpoint with the exact repair and next action, reopen affected checklist items, rerun fanout, verify again.

## Constraints

- **Goal approval gate**: show `goal.md` to the user and pause until approved; it is then the contract — changes to its outcome or constraints need renewed approval.
- Do not create specification documents, test scripts, or other artifacts unless the approved goal requires them.
- One whole-goal plan and one executor call by default. Split only when one executor cannot safely finish within the 7200-second / 470,000-token budget or when genuinely independent streams give useful concurrency; inputs alone near that budget force one.
- Never edit `plans.json` by hand — `casper_status.py` is its only writer.
- A `NEEDS-USER:` is answered by the user, never by you: ask, replace the line with the answer under any other prefix (e.g. `USER-ANSWER: <answer>`), update the next action, rerun fanout; only the latest line counts and fanout never redispatches while one stands.
- Report unresolved criteria or `NEEDS-USER` blockers rather than claiming success.
- `$HD` is transient run state, not a deliverable: deferred-work notes belong in the target repo's `.agents/handovers/`, never here. Clean up in the same session the result is reported — the run is unfinished while `$HD` exists: `"$PY" "$WF/casper_cleanup.py" --handover-dir "$HD"` (refuses while anything is unresolved or unverified). `--force` is only for a goal the user explicitly abandoned and still enforces the shape check. Never rename `$HD` (`-archived`, `-superseded`, …) in place of deleting it: a rename retires nothing and only hides the dir. Reap leftovers with `casper_cleanup.py --sweep "$HOME/.agents/state/casper" --min-age-days 7` (`--dry-run` first).
- **Host gate**: fanout launches a resolver only while `MemAvailable` >= `--min-free-gb` (default 4 GiB), waiting up to `--host-wait` seconds (default 900), otherwise recording `skipped_host_busy` and leaving the plan open for a later re-run. RAM here swings ~15 GB within an hour; `--min-free-gb 0` disables the gate, only for a host you measured yourself.
- **On wake, act at once (resume checklist)**: re-read this file from disk (it wins over context); reuse the approved `$HD/goal.md`; a live fanout (launch-block `pgrep`) or `claimed_elsewhere` in `fanout-result.json` means another fanout holds the lease — watch that PID, never relaunch; inspect `plans.json`, checkpoints, `fanout-result.json`, log tails; surface and resolve any `NEEDS-USER:` before dispatch; triage paused/failed plans; re-run fanout (detached, fresh watcher) while some open plan has no `NEEDS-USER:` — when all carry one, fanout only re-pauses them: wait for the user; verify when all plans are done; clean up only on all-pass, else keep `$HD`.

## Verify

```bash
"$PY" "$WF/casper_status.py" --health --handover-dir "$HD"
[ ! -d "$HD" ] && echo cleaned # after cleanup only
```

Exit `0` = healthy; exit `1` prints each failing check (nothing-to-check once `$HD` is gone).
