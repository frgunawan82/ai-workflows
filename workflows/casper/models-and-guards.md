# Models, effort, and guards

Helper for [`casper.md`](casper.md): harness model routing, backend selection, credentials, effort defaults, and the dispatch guards.

## Model and effort defaults

`LLM_harness.sh --list-models` prints accepted aliases. The default model is **driver-aware**: fanout chooses each plan's non-empty `plans.json` `model`, then `--model`, then the derived default — under a pi driver (`PI_CODING_AGENT` with `PI_PROVIDER` and `PI_MODEL` all set) that is the driving session's own model composed as `PROVIDER/ID` (e.g. `anthropic/claude-opus-5`, the pi route on the driver's credentials); anywhere else it is `opus` (Claude Code). `LLM_harness.sh --default-model` prints the derived default; omitting fanout/verifier `--model` means auto. The bare `PI_MODEL` is deliberately never used alone: an unqualified `claude-*` id would route to the Claude CLI, silently jumping drivers. The harness and `casper_fanout.py` implement this rule in lockstep (parity-tested). The harness uses a 7200-second hard timeout. Unless effort is explicitly overridden, GPT 5.6 Sol uses `high` (bare or provider-qualified) and every other model — Opus 5 included — falls back to `medium`. Use harness `--thinking`, fanout/verifier `--effort`, or a non-empty per-plan `plans.json` `effort` to override the default; per-plan effort takes precedence in fanout. Which CLI runs the call is a separate choice with its own precedence — see Backend selection below; the model, the effort and the backend are never derived from each other.

Every harness call casper makes is an executor (fanout resolver) or verifier call, so `medium` is the implementation default. Plan authoring is done by the driving session at `xhigh`; any ad-hoc planning harness call must pass `--thinking xhigh` explicitly.

## Backend selection (`--backend auto|pi|claude`)

The backend is an axis of its own — independent of the model, the effort and the profile — on `LLM_harness.sh`, `casper_fanout.py` and `casper_verify.py`, plus an optional per-plan `backend` in `plans.json`. Fanout precedence is **non-empty per-plan `backend` > `--backend` > `auto`**, decided separately from the model/effort precedence, and the selected value is forwarded to the harness, which routes accordingly; the verifier forwards it to the judgment call (and omits the flag when it is `auto`). Every entry point rejects an unknown value with exit `2`.

- **`auto` (default)** is exactly the historical rule described below: the model shape plus the `PI_CODING_AGENT` driver sniff. Nothing about default dispatch changes.
- **`pi`** forces the pi route even outside a driver: a bare `claude*` id becomes `anthropic/<id>`, a bare non-Claude id keeps `--provider openai`, and a provider-qualified id is passed through untouched (never re-qualified, never stripped).
- **`claude`** forces the Claude CLI even under a pi driver, on **its own** auth context (`$CLAUDE_CONFIG_DIR`, default `~/.claude`); no pi profile is read, inferred or mapped, and no `PI_*` variable is injected. It accepts an unqualified lowercase `claude*` id (aliases resolved) and `anthropic/claude*` — the one provider prefix naming the same models, which is dropped. Any other model — another provider's id (`openai-codex/…`, `claude/claude-opus-5`) or a non-Claude id — exits `2` with a message naming the model, instead of silently switching model or provider. With no `--model` under a GPT pi driver the derived default is the driver's GPT id, so `--backend claude` alone exits `2` asking for a Claude model rather than quietly running something else.

Fanout validates the selected, still-unresolved (backend, model, token budget) triples **before it claims a lease or edits any plan doc**, and exits `2` naming the offending plan file — so a mistyped per-plan `backend` in a mixed batch costs no lease, no status change and no checkpoint rewrite.

```bash
"$WF/LLM_harness.sh" --backend claude --model opus --max-context-tokens 470000 -- "<prompt>"  # Claude CLI + token guard, even under pi
"$WF/LLM_harness.sh" --backend pi --model opus -- "<prompt>"                                  # anthropic/claude-opus-5 on pi, anywhere
"$PY" "$WF/casper_fanout.py" --handover-dir "$HD" --backend claude --model sonnet             # every plan on the Claude CLI
"$PY" "$WF/casper_verify.py" --handover-dir "$HD" --backend claude --model opus               # judgments on the Claude CLI
```

A session id belongs to the CLI that minted it, so the ledger records `session_backend` (`claude` | `pi`) next to `session` and a dispatch warm-resumes only a session tagged with the backend it is about to run — a pi session id is never handed to `claude --resume`, and a claude id is never reused as a pi session id. Provenance is written and cleared with the id itself (including a manifest re-init that drops it). A **legacy untagged** id from before this field existed cold-starts **once**; that run re-records the id with its tag, so warm resume returns from the next pause onwards.

## Provider routing and credentials

On the default `auto` backend, after alias resolution, only unqualified lowercase `claude*` IDs use the normal authenticated `claude -p --dangerously-skip-permissions` — and only outside pi. **Under a pi driver (`PI_CODING_AGENT` set) every unqualified `claude*` ID, aliases included, is qualified as `anthropic/<id>` and runs on pi**, so resolvers use the driver's own credentials: pi reads `$PI_CODING_AGENT_DIR/auth.json`, i.e. the `pi-<profile>` that launched the driver, and that variable is inherited through the bash tool, jobs, subagents, fanout and the pi guard. The Claude CLI instead reads `$CLAUDE_CONFIG_DIR` (default `~/.claude`), which no pi profile sets, so routing there from pi would silently jump to the default Claude account. This switches those resolvers to the pi backend (pi guard, pi-session warm resume, no token guard). A bare non-Claude ID (including a mixed-case unqualified `Claude*` ID) uses Pi's OpenAI API provider (`pi -p --provider openai --model ID`), while every provider-qualified ID—including `claude/...` and `anthropic/...`—uses that configured Pi provider directly (`pi -p --model PROVIDER/ID`). For example:

```bash
"$WF/LLM_harness.sh" --model opus --stopwatch 7200 -- "<prompt>"                        # medium (executor)
"$WF/LLM_harness.sh" --model opus --thinking xhigh --stopwatch 7200 -- "<plan prompt>"   # xhigh (planning)
"$WF/LLM_harness.sh" --model gpt-5.6-sol --stopwatch 7200 -- "<prompt>"                  # high
"$WF/LLM_harness.sh" --model gpt-5.6-sol --thinking max --stopwatch 7200 -- "<prompt>"   # override
```

The Claude route requires an authenticated Claude Code profile. The two `gpt-5.6-sol` forms intentionally use different credentials: bare `gpt-5.6-sol` stays on Pi's OpenAI API route (`--provider openai`) and requires `OPENAI_API_KEY` or a saved OpenAI API-key credential; it is not remapped to Codex. `openai-codex/gpt-5.6-sol` is passed provider-qualified with no extra `--provider` and uses the locally configured Pi ChatGPT OAuth/subscription credential (created through Pi's `/login`). The harness sends `SIGTERM` at the timeout and `SIGKILL` ten seconds later if needed.

## Warm resume and zero-progress stops

Both backends warm-resume their own paused plans — the main saving when answering a `NEEDS-USER` — and each recorded id carries its `session_backend` tag, so only a dispatch on that same backend resumes it (see Backend selection). **Claude**: when a plan pauses with token headroom (context below the wind-down line), fanout records the resolver's session id from the guard sidecar (tagged `claude`) and the next dispatch re-enters it (`claude --resume`) with a short continuation prompt; token-full sessions restart cold, and an unresumable session falls back to a cold start automatically. **Pi**: fanout mints a per-attempt session id, dispatches with `--pi-session-id`/`--pi-session-dir` so the session lives in `$HD/pi-sessions/` (cleaned up with the handover), and records the id (tagged `pi`) on a pause once pi's session file actually exists (an id tagged for the other backend, an untagged legacy id, or a run that died before pi started, cold-starts the full prompt instead); the harness resumes by session **file** (`pi --session <file>`), which works from any cwd — plain `pi --session-id` lookup is project(cwd)-scoped and would miss the session on a re-run from another directory. If the recorded id's file is gone, the harness cold-creates the session under that id and the resume prompt has the resolver re-read the plan doc. On either backend `failed` plans always restart cold (fresh session) — except a pi run whose session ends on a terminal provider usage/rate-limit error (e.g. "Codex error: The usage limit has been reached", HTTP 429): fanout records that as a **pause** with `pause_reason: provider-limit` so the next round warm-resumes the intact session instead of cold-restarting and escalating effort, and pi needs no token-fullness veto: pi auto-compacts as context approaches the model window.

If the token budget hard-stops a resolver's very first model call, the run made zero progress: the plan's inputs alone may not fit the budget, and retrying cannot help. Fanout counts consecutive zero-progress stops in the ledger and on the second one records a `NEEDS-USER:` in the checkpoint asking to split the plan (or slim its inputs, or raise `--max-context-tokens`); the plan is not redispatched until that line is resolved.

## Token cap (`--max-context-tokens`)

The token guard lives in `casper_guard.py`, which only drives the Claude CLI, so the cap is **claude-only** and never silently dropped:

| Flag | Effective claude backend | Effective pi backend |
| --- | --- | --- |
| omitted (fanout) | historical `470000` budget | no cap (pi has no token accounting here) |
| omitted (harness) | no cap (harness default is `0`) | no cap |
| explicit `0` | guard off | guard off |
| explicit nonzero | guard on at that budget | **exit `2`** naming the offending plan (fanout) or the resolved route (harness) |

Examples: `casper_fanout.py --handover-dir "$HD"` keeps 470000 on Claude plans and no cap on pi plans in the same batch; `--max-context-tokens 300000 --backend claude` caps every plan; `--max-context-tokens 0` runs uncapped on both; `--max-context-tokens 300000` with any pi-resolving plan in scope exits `2` before dispatch, so a cap you asked for is never quietly ignored. The guard is chosen by the **effective backend**, not by the model text.

## Guards

When a dispatch's effective backend is claude and `--max-context-tokens` is nonzero, the resolver runs under `casper_guard.py`: it watches per-call token usage in stream-json mode, injects a wind-down notice at `MAXCTX−40000` tokens (or 300 seconds before the stopwatch) telling the resolver to checkpoint and stop, and hard-stops with exit `124` at either budget. The wall-clock stopwatch remains the backstop for hung Claude sessions. Pi needs no token hard-stop — pi auto-compacts as context approaches the model window — and every guarded Pi dispatch (harness `--pi-stall-secs` > 0, default 900) runs under `casper_pi_guard.py` in **rpc mode**: it drives `pi --mode rpc` over the JSONL protocol, sends the resolver prompt as an rpc command, treats the event stream as liveness (with process-tree CPU/io and session-file writes as fallback for quiet tool calls), steers the same self-contained wind-down notice harness `--grace` (300) seconds before the stopwatch (the guard's own `--grace` is the 60-second stall re-arm) at a clean turn boundary, sends a clean `abort` at the stopwatch (ten seconds to settle, then the tree is killed with exit `124`), auto-cancels extension UI dialogs (matching headless `pi -p`, which blocks them outright), and prints the **final assistant text** on stdout — exactly what `casper_verify.py` parses. A wind-down window that does not fit inside the stopwatch is skipped. On a stall it first SIGTERMs wedged *leaf* pi subagent processes (a hung streaming call usually resumes within seconds once the leaf dies); if the run stays silent, it kills the whole tree with exit `124`, which fanout records as a **pause** (never a fail — a stall must not escalate effort), and the next fanout run redispatches it. `--pi-stall-secs 0` is the escape hatch: bare `pi -p`, stdout byte-for-byte the model's output, no guard. Guard actions appear as `[pi-guard]` lines in the plan log. The pi guard's `--session-root` defaults to `$PI_CODING_AGENT_DIR` (else `~/.pi/agent`), and the harness passes `--watch-dir` for the fanout session dir so resolver session writes under `$HD/pi-sessions/` count as liveness.
