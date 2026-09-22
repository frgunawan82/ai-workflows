#!/usr/bin/env python3
"""Status-aware parallel dispatch of plan documents to headless LLM resolvers.

Reads the ledger (plans.json) in a handover dir, launches one LLM_harness.sh per
OPEN plan in parallel (per wave), captures each to logs/<plan>.log, then records the
outcome back into the ledger. Idempotent: `done` plans are skipped and a re-run
resumes only what is left — so multiple sessions can pick up where work left off.

Waves gate on completion: if any plan in a wave does not reach `done`, later waves
are NOT dispatched (they stay open in the ledger and are reported as
`skipped_dependency` in fanout-result.json; a re-run picks them up once the earlier
wave is done). Passing --wave N limits dispatch scope but still honors lower-wave gates.

A plan is also left open and unclaimed (`skipped_host_busy`) when the host has less
than --min-free-gb of MemAvailable and does not free up within --host-wait seconds.

Exit codes:
  0  every plan in scope is done
  1  one or more plans paused/failed/skipped, incl. skipped_host_busy (re-run to resume)
  2  usage error, including a selected plan whose backend/model/token-budget combination
     cannot run — reported before anything is claimed or modified
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import casper_status as cs  # bundled sibling; script dir is on sys.path

HERE = Path(__file__).resolve().parent
DEFAULT_HARNESS = HERE / "LLM_harness.sh"
STATUS_SCRIPT = HERE / "casper_status.py"

RESOLVER_PROMPT = """\
Resolve the whole approved work unit in @{plan_path} (goal: @{goal_path}) in this ONE session.
You have approximately {ckpt} seconds before the hard stop.{budget_note}

- Do the actual work described by the goal and plan. Prefer the correct long-term solution,
  then the simplest solution that can be improved later. Do not create specification or test
  artifacts unless the approved goal/plan actually requires them.
- You run headless: ending your turn terminates this process immediately, and nothing
  re-invokes you when a background task finishes. Never end your turn to "wait" for anything.
  Run long commands in the foreground of a single tool call, or launch them detached
  (`setsid nohup ... & disown`) and poll for completion inside your tool calls before the
  turn ends.
- If the plan has a checklist, work every unchecked item in order and mark completed items.
  Otherwise, complete its Objective/Work contract as a whole. Run relevant focused checks.
- Decisions only the user can make (irreversible/destructive, or not derivable from the goal,
  plan, and code) must not be guessed. Record exactly ONE open line beginning
  "NEEDS-USER: " with the question and concrete options, finish independent safe work, and stop.
- "## Progress / Handover" is a bounded REPLACEMENT checkpoint, not a journal. Before stopping,
  replace its existing content with at most {checkpoint_max} characters covering only: completed
  work, current evidence/failure, remaining work, exact next action, and the single NEEDS-USER
  record when blocked. Never append historical attempts or long command output there.

When the whole plan is complete, mark it resolved by running exactly:
  "{py}" "{status_script}" --set "{plan_file}" done --handover-dir "{handover_dir}"

If unfinished or timed out, leave the replacement checkpoint accurate and stop. A later resolver
will continue from that compact state.
"""

# Used instead of RESOLVER_PROMPT when re-dispatching a paused plan into its own
# prior session (claude --resume): the original instructions are already in context,
# so only the delta — re-read the plan doc, continue — needs restating.
RESUME_PROMPT = """\
You are RESUMING your previous session on @{plan_path} (goal: @{goal_path}); its context above
is still yours. The plan document may have changed since you stopped (for example an answered
NEEDS-USER line). Re-read @{plan_path} now, then continue from its exact next action under your
original instructions, including the "## Progress / Handover" replacement-checkpoint contract.
You have approximately {ckpt} seconds before the hard stop.{budget_note}

When the whole plan is complete, mark it resolved by running exactly:
  "{py}" "{status_script}" --set "{plan_file}" done --handover-dir "{handover_dir}"
"""

# Appended to the stopwatch line of RESOLVER_PROMPT when the token guard is on.
BUDGET_NOTE = """
You also have a context budget: a "[CASPER GUARD — WIND-DOWN NOTICE]" user message arrives
near {soft:,} tokens (hard stop at {hard:,}) or near the time limit. Obey it immediately:
replace the checkpoint, run the done command below only if the plan is truly complete, and
end your turn. Budgets can end a session abruptly — refresh the "## Progress / Handover"
checkpoint after each substantial milestone so an abrupt stop loses little."""

_PROGRESS_RE = re.compile(r"(?ms)^## Progress / Handover\s*\n(.*?)(?=^##\s|\Z)")
_NEEDS_USER_PREFIX = "NEEDS-USER: "
_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_CLAUDE_ALIASES = {
    "fable", "fable-5", "fable5",
    "opus", "opus-5", "opus5",
    "sonnet", "sonnet-5", "sonnet5",
    "haiku", "haiku-4.5", "haiku-4-5",
}
_GPT_56_SOL_MODELS = {"sol", "gpt-5-6-sol"}
_BACKENDS = ("auto", "pi", "claude")
# Historical claude token budget, applied only when --max-context-tokens is
# OMITTED and the plan actually resolves to the claude backend. An omitted flag
# on pi means "no cap" (pi has no token guard here), so the automatic pi route
# keeps working; an explicit 0 disables the guard on either backend.
DEFAULT_CLAUDE_CONTEXT_TOKENS = 470000


def _default_model() -> str:
    """Mirror LLM_harness.sh default_model(): the driver-aware default.

    Under a pi driver (PI_CODING_AGENT with PI_PROVIDER and PI_MODEL set)
    resolvers inherit the driving session's own model as a provider-qualified
    id — never the bare PI_MODEL, so the driver's actual provider is kept (a
    bare claude-* id would be re-qualified as anthropic/ by the harness).
    Anywhere else the historical Claude default stands.
    Kept in lockstep with the harness by a parity test.
    """
    if (os.environ.get("PI_CODING_AGENT")
            and os.environ.get("PI_PROVIDER") and os.environ.get("PI_MODEL")):
        return f"{os.environ['PI_PROVIDER']}/{os.environ['PI_MODEL']}"
    return "opus"


def _pi_session_exists(hd: Path, session_id: str) -> bool:
    """True when pi actually wrote this session under the handover's session dir.

    Gates warm resume AND pause-time recording: a ledger id without a matching
    session file is stale (recorded by another backend, or the run died before
    pi started) and must cold-start the full resolver prompt instead.
    """
    return any((hd / "pi-sessions").glob(f"*_{session_id}.jsonl"))


_PROVIDER_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|too many requests|\b429\b|quota", re.IGNORECASE)


def _pi_provider_limit(hd: Path, session_id: str) -> bool:
    """True when the pi session's LAST assistant message died on a provider limit.

    A resolver killed by a usage/rate limit (e.g. "Codex error: The usage limit
    has been reached", HTTP 429) exits non-zero, which _status_for records as
    failed — a cold restart that discards the intact session and escalates
    effort. The limit is transient, so such a run must pause and warm-resume
    instead. Only a TERMINAL limit error counts: any later successful assistant
    message means pi recovered on its own and the exit had another cause.
    """
    files = sorted((hd / "pi-sessions").glob(f"*_{session_id}.jsonl"))
    if not files:
        return False
    last_error = None
    try:
        with open(files[-1], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                if msg.get("stopReason") == "error":
                    last_error = str(msg.get("errorMessage") or "")
                else:
                    last_error = None  # recovered: a later message succeeded
    except OSError:
        return False
    return bool(last_error and _PROVIDER_LIMIT_RE.search(last_error))


def _model_key(model: str) -> str:
    """Normalize a harness alias or provider-qualified model id for matching."""
    unqualified = model.strip().lower().rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "-", unqualified).strip("-")


def _is_claude_model(model: str, backend: str = "auto") -> bool:
    """Mirror LLM_harness.sh: does this (model, backend) pair run on the Claude CLI?

    `backend` is the selected axis, independent of the model:
      claude - always the Claude CLI (the pair is validated by _claude_accepts);
      pi     - always pi, whatever the model looks like;
      auto   - the historical rule: only aliases and unqualified lowercase
               claude* use Claude, and never under a pi driver (PI_CODING_AGENT
               set), where the harness qualifies a bare claude-* id as
               anthropic/<id> so it runs on pi with the driver's own credentials
               ($PI_CODING_AGENT_DIR/auth.json, the pi-<profile> that launched
               the driver) instead of the Claude CLI's separately logged-in
               default profile.
    Kept in lockstep with the harness (follow_driver/qualify_*) by a parity test.
    """
    if backend == "claude":
        return True
    if backend == "pi":
        return False
    if "/" in model or os.environ.get("PI_CODING_AGENT"):
        return False
    normalized = model.lower().replace(" ", "-")
    return normalized in _CLAUDE_ALIASES or model.startswith("claude")


def _claude_accepts(model: str) -> bool:
    """Mirror LLM_harness.sh qualify_claude(): can `--backend claude` run this model?

    Accepts an unqualified lowercase claude* id (harness aliases included) and
    anthropic/claude*, the one provider prefix naming the same models. Any other
    provider-qualified id or non-Claude id is rejected so an explicit backend
    never silently switches the user's model or provider.
    """
    m = model.strip()
    if m.lower().replace(" ", "-") in _CLAUDE_ALIASES:
        return True
    if m.startswith("anthropic/"):
        m = m[len("anthropic/"):]
    return "/" not in m and m.startswith("claude")


def _selected_backend(entry: dict, cli_backend: str) -> str:
    """Backend precedence: non-empty per-plan `backend`, then --backend, then auto.

    Independent of the model/effort precedence above it: a plan may pin its
    backend without pinning a model, and vice versa.
    """
    return str(entry.get("backend") or cli_backend or "auto").strip()


def _context_budget(max_context_tokens: int | None, claude_backend: bool) -> int:
    """Token budget for one dispatch; 0 means the token guard is off.

    An omitted --max-context-tokens keeps the historical 470000 default for the
    claude backend only; on pi it means "no cap" (there is no pi token guard).
    An explicit value is honored on claude and rejected up front on pi.
    """
    if not claude_backend:
        return 0
    if max_context_tokens is None:
        return DEFAULT_CLAUDE_CONTEXT_TOKENS
    return max_context_tokens


def _validate_selection(entries: list[dict], cli_backend: str, cli_model: str | None,
                        max_context_tokens: int | None) -> list[str]:
    """Errors for every unresolved plan whose backend/model/budget cannot run.

    Runs before any lease is taken and before any plan doc is rewritten, so a
    mistyped backend or an unhonorable token cap never costs a claim, a status
    change or a checkpoint edit. Only plans that would actually be dispatched
    are checked, using the same precedence dispatch uses, and each message names
    the offending plan file.
    """
    errors: list[str] = []
    for e in entries:
        name = e.get("file", "?")
        backend = _selected_backend(e, cli_backend)
        if backend not in _BACKENDS:
            errors.append(f"{name}: backend must be one of {'|'.join(_BACKENDS)}, "
                          f"got {backend!r} (per-plan 'backend' in plans.json)")
            continue
        model = e.get("model") or cli_model or _default_model()
        explicit_model = bool(e.get("model") or cli_model)
        if backend == "claude" and not _claude_accepts(model):
            source = "" if explicit_model else " (the derived default model)"
            errors.append(
                f"{name}: backend 'claude' cannot run model {model!r}{source} — the "
                "Claude CLI takes claude* or anthropic/claude* ids only. Set a Claude "
                "model for this plan (--model, or the plan's 'model'), or drop the "
                "claude backend to keep this model")
            continue
        if max_context_tokens and not _is_claude_model(model, backend):
            errors.append(
                f"{name}: --max-context-tokens {max_context_tokens} needs the claude "
                f"backend, but this plan resolves to pi (backend {backend!r}, model "
                f"{model!r}); pi has no token guard. Use backend 'claude' or "
                "--max-context-tokens 0")
    return errors


def _effective_effort(configured: str | None, prior_status: str,
                      model: str = "") -> str:
    """Choose the model default, then increase one level after a recorded failure."""
    if configured:
        effort = configured.lower()
    elif _model_key(model) in _GPT_56_SOL_MODELS:
        effort = "high"
    else:
        effort = "medium"
    if prior_status != "failed" or effort not in _EFFORTS:
        return effort
    return _EFFORTS[min(_EFFORTS.index(effort) + 1, len(_EFFORTS) - 1)]


def _compact_checkpoint(plan_path: Path, max_chars: int) -> bool:
    """Replace the progress section with a bounded checkpoint; return open-user state."""
    try:
        text = plan_path.read_text()
    except OSError:
        return False
    match = _PROGRESS_RE.search(text)
    body = match.group(1).strip() if match else ""
    needs = [line.strip() for line in body.splitlines()
             if line.lstrip().startswith(_NEEDS_USER_PREFIX)]
    need = needs[-1] if needs else ""
    ordinary = "\n".join(line for line in body.splitlines()
                           if not line.lstrip().startswith(_NEEDS_USER_PREFIX)).strip()
    if len(need) > max_chars:
        need = need[:max_chars].rstrip()
    prefix = f"{need}\n" if need else ""
    room = max(max_chars - len(prefix), 0)
    if len(ordinary) > room:
        marker = "[checkpoint truncated]\n"
        keep = max(room - len(marker), 0)
        ordinary = marker + (ordinary[-keep:] if keep else "")
    compact = (prefix + ordinary).strip()[:max_chars]
    replacement = f"## Progress / Handover\n{compact}\n"
    if match:
        new = text[:match.start()] + replacement + text[match.end():]
    else:
        new = text.rstrip() + "\n\n" + replacement
    if new != text:
        tmp = plan_path.with_suffix(plan_path.suffix + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, plan_path)
    return any(line.lstrip().startswith(_NEEDS_USER_PREFIX)
               for line in compact.splitlines())


def _read_state(state_path: Path) -> dict:
    """The guard's sidecar for the last run ({} when absent/corrupt: legacy routes)."""
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _warm_session(state: dict, max_context_tokens: int,
                  context_grace: int) -> tuple[str | None, str | None]:
    """(session, reason) to record on a pause; session only when warm-resumable.

    The guard's sidecar reports the session id, stop reason, and final context
    gauge. A session at or past the wind-down line must NOT be resumed — it
    would re-trip the token budget almost immediately — so it restarts cold.
    """
    reason = state.get("reason")
    session = state.get("session")
    if not session:
        return None, reason
    gauge = int(state.get("gauge") or 0)
    if max_context_tokens and gauge >= max_context_tokens - context_grace:
        return None, reason
    return session, reason


# Consecutive first-call-over-budget stops tolerated before a plan is flagged for
# the user. The first stop discovers the condition; the second confirms it is
# deterministic (plan inputs alone exceed the budget) — retrying further only
# burns a full-window API call per round without ever progressing.
_ZERO_PROGRESS_LIMIT = 2

_OVER_BUDGET_ASK = (
    "plan input alone appears to exceed the {budget:,}-token context budget "
    "({count} consecutive zero-progress stops). Options: split this plan into smaller "
    "self-contained plans; slim the goal/plan or the files they reference; or rerun "
    "fanout with a higher --max-context-tokens. Replace this line with the chosen "
    "resolution, then rerun fanout.")


def _flag_over_budget(plan_path: Path, count: int, budget: int, max_chars: int) -> None:
    """Record a NEEDS-USER in the checkpoint so the pause machinery stops the loop."""
    try:
        text = plan_path.read_text()
    except OSError:
        return
    line = _NEEDS_USER_PREFIX + _OVER_BUDGET_ASK.format(budget=budget, count=count)
    match = _PROGRESS_RE.search(text)
    if match:
        body = match.group(1).rstrip()
        section = f"## Progress / Handover\n{body}\n{line}\n" if body \
            else f"## Progress / Handover\n{line}\n"
        new = text[:match.start()] + section + text[match.end():]
    else:
        new = text.rstrip() + f"\n\n## Progress / Handover\n{line}\n"
    tmp = plan_path.with_suffix(plan_path.suffix + ".tmp")
    tmp.write_text(new)
    os.replace(tmp, plan_path)
    _compact_checkpoint(plan_path, max_chars)  # bound it; ours is the latest NEEDS-USER


def _mem_available_gb(meminfo_path: str = "/proc/meminfo") -> float | None:
    """Free-for-allocation RAM in GiB, or None when the probe itself is unreadable."""
    try:
        with open(meminfo_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576  # kB -> GiB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _wait_for_host(min_free_gb: float, max_wait: int, poll: int = 30,
                   probe=_mem_available_gb, sleep=time.sleep,
                   label: str = "") -> bool:
    """True once the host has the RAM headroom to take another resolver.

    A resolver is a full agent session; launching one onto a box that is already
    out of memory gets it SIGKILLed mid-flight. Gate on MemAvailable only (load
    average says nothing about the failure mode) and never block on a broken
    probe: an unreadable /proc/meminfo passes the gate open. Elapsed time is
    counted from the sleeps, so the bound holds regardless of wall clock.
    """
    if min_free_gb <= 0:
        return True
    free = probe()
    if free is None or free >= min_free_gb:
        return True
    print(f"[fanout] host busy: MemAvailable {free:.1f} GiB < {min_free_gb} GiB — "
          f"waiting up to {max_wait} s{' for ' + label if label else ''}",
          file=sys.stderr)
    elapsed = 0
    while elapsed < max_wait:
        sleep(poll)
        elapsed += poll
        free = probe()
        if free is None or free >= min_free_gb:
            return True
    return False


def _status_for(exit_code: int, already_done: bool, needs_user: bool = False) -> str:
    """Derive the ledger status from the resolver's exit code AND its own signal.

    The resolver's own `casper_status.py --set <plan> done` is the source of truth,
    whatever the exit code: a resolver that finished and marked done can still be
    stopwatch-killed (124/137) or exit non-zero afterwards — that must not reopen
    the plan. Conversely, exit code 0 alone is not proof of completion: a
    `claude -p` session can end its turn for benign reasons (e.g. hitting its
    internal background-task wait ceiling) without ever marking done, so a clean
    exit without the resolver's signal means "stopped without finishing" (paused).
    """
    if needs_user:
        return "paused"
    if already_done:
        return "done"
    if exit_code in (124, 137):  # SIGTERM/SIGKILL from the stopwatch
        return "paused"
    if exit_code == 0:
        return "paused"
    return "failed"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fan plan docs out to parallel LLM resolvers.")
    ap.add_argument("--handover-dir", required=True, type=Path)
    ap.add_argument("--stopwatch", type=int, default=7200, help="Hard per-resolver budget (s)")
    ap.add_argument("--grace", type=int, default=300, help="Checkpoint margin before the kill (s)")
    ap.add_argument("--slack", type=int, default=300, help="Extra margin before a lease is stale (s)")
    ap.add_argument("--checkpoint-chars", type=int, default=4000,
                    help="Maximum Progress / Handover checkpoint size")
    ap.add_argument("--max-context-tokens", type=int, default=None,
                    help="Hard per-resolver context-token budget; claude backend only "
                         f"(omitted: {DEFAULT_CLAUDE_CONTEXT_TOKENS} on claude, no cap on "
                         "pi; 0 disables the token guard; a nonzero value on a plan that "
                         "resolves to pi is an error)")
    ap.add_argument("--context-grace", type=int, default=40000,
                    help="Token margin before the hard limit at which the wind-down "
                         "notice is injected")
    ap.add_argument("--model", default=None,
                    help="Override model for all plans (alias like sonnet/opus or full id; "
                         "resolved by LLM_harness.sh)")
    ap.add_argument("--effort", default=None, help="Override thinking/effort for all plans")
    ap.add_argument("--backend", choices=_BACKENDS, default="auto",
                    help="Backend for all plans (auto: the harness's model/driver rule); "
                         "a non-empty per-plan 'backend' in plans.json wins over this")
    ap.add_argument("--min-free-gb", type=float, default=4.0,
                    help="Minimum MemAvailable before a resolver is launched; "
                         "0 disables the host gate")
    ap.add_argument("--host-wait", type=int, default=900,
                    help="Seconds to wait for that headroom before skipping the plan")
    ap.add_argument("--wave", type=int, default=None, help="Limit to a single wave")
    ap.add_argument("--harness", type=Path, default=DEFAULT_HARNESS)
    args = ap.parse_args()
    if (args.stopwatch <= 0 or args.grace < 0 or args.slack < 0
            or args.checkpoint_chars <= len(_NEEDS_USER_PREFIX)):
        ap.error("stopwatch must be positive; grace/slack must be non-negative; "
                 "checkpoint-chars must fit a NEEDS-USER marker")
    budget_for_grace = (DEFAULT_CLAUDE_CONTEXT_TOKENS if args.max_context_tokens is None
                        else args.max_context_tokens)
    if (args.max_context_tokens is not None and args.max_context_tokens < 0) or (
            budget_for_grace and not 0 <= args.context_grace < budget_for_grace):
        ap.error("max-context-tokens must be >= 0; context-grace must satisfy "
                 "0 <= context-grace < max-context-tokens")
    if args.min_free_gb < 0 or args.host_wait < 0:
        ap.error("min-free-gb and host-wait must be non-negative")

    hd: Path = args.handover_dir
    if not cs.ledger_path(hd).exists():
        print(f"no ledger at {cs.ledger_path(hd)} — run casper_status.py --init first", file=sys.stderr)
        return 2
    (hd / "logs").mkdir(parents=True, exist_ok=True)
    stale_secs = args.stopwatch + args.grace + args.slack
    ckpt = max(args.stopwatch - args.grace, 60)
    plans = cs._load_raw(hd)
    selected = [e for e in plans
                if args.wave is None or int(e.get("wave", 0)) == args.wave]
    if not selected:
        print("nothing in scope")
        return 0
    if all(e.get("status", "pending") == "done" for e in selected):
        print("nothing open to dispatch; every plan in scope is done")
        return 0

    # Validate the selected, still-unresolved (backend, model, budget) triples
    # BEFORE any lease or plan mutation: an impossible pair must cost nothing.
    errors = _validate_selection(
        [e for e in selected if e.get("status", "pending") != "done"],
        args.backend, args.model, args.max_context_tokens)
    if errors:
        for message in errors:
            print(f"casper_fanout.py: {message}", file=sys.stderr)
        return 2

    waves = sorted({int(e.get("wave", 0)) for e in selected})
    results: list[dict] = []

    for wave in waves:
        # Dependencies come from the complete ledger, not merely from plans that
        # happen to be dispatchable in this process. A live lower-wave lease is
        # unresolved and must gate this wave just like a pause or failure.
        ledger = cs._load_raw(hd)
        lower_unfinished = [e for e in ledger
                            if int(e.get("wave", 0)) < wave
                            and e.get("status", "pending") != "done"]
        if lower_unfinished:
            for e in selected:
                w = int(e.get("wave", 0))
                if w >= wave and e.get("status", "pending") != "done":
                    results.append({"file": e["file"], "wave": w, "exit_code": None,
                                    "status": "skipped_dependency", "log": None})
            print(f"lower wave has unfinished plan(s) — skipping wave {wave} and later; "
                  f"re-run once it is done", file=sys.stderr)
            break

        batch = [e for e in selected if int(e.get("wave", 0)) == wave]
        running = []  # entry/process/log metadata plus backend budget state
        for e in batch:
            current = cs._find(cs._load_raw(hd), e["file"])
            if not current or current.get("status", "pending") == "done":
                continue
            if not cs.is_open(current, stale_secs):
                results.append({"file": e["file"], "wave": wave, "exit_code": None,
                                "status": "claimed_elsewhere", "needs_user": False,
                                "effort": None, "log": None})
                continue
            plan_path = hd / e["file"]
            # Compact first. The pause decision must reflect the bounded result,
            # including for a checkpoint that was already paused on entry.
            if _compact_checkpoint(plan_path, args.checkpoint_chars):
                cs.set_status(hd, e["file"], "paused")
                results.append({"file": e["file"], "wave": wave, "exit_code": None,
                                "status": "paused", "needs_user": True,
                                "effort": None, "log": None})
                continue  # user answer must replace NEEDS-USER before any re-dispatch
            # Gate on host RAM before claiming: a gated plan must stay open and
            # unleased so a later run picks it up (a NEEDS-USER pause above must
            # never wait for the host first).
            if not _wait_for_host(args.min_free_gb, args.host_wait,
                                  label=e["file"]):
                results.append({"file": e["file"], "wave": wave, "exit_code": None,
                                "status": "skipped_host_busy", "needs_user": False,
                                "effort": None, "log": None})
                print(f"insufficient free RAM for {e['file']} — leaving it open; "
                      f"re-run fanout later", file=sys.stderr)
                continue
            if cs.claim(hd, e["file"], stale_secs) != "claimed":
                current = cs._find(cs._load_raw(hd), e["file"])
                if current and current.get("status") != "done":
                    results.append({"file": e["file"], "wave": wave, "exit_code": None,
                                    "status": "claimed_elsewhere", "needs_user": False,
                                    "effort": None, "log": None})
                continue
            # Model precedence is per-plan, then CLI override, then the driver-
            # aware default (the pi driver's own model under pi, otherwise Opus).
            # Backend precedence is the same shape but independent: per-plan,
            # then --backend, then auto (the harness's model/driver rule).
            # The effective backend alone decides token-budget prompts/state vs
            # pi session flags, and each backend warm-resumes only its own
            # paused sessions.
            model = e.get("model") or args.model or _default_model()
            backend = _selected_backend(e, args.backend)
            claude_backend = _is_claude_model(model, backend)
            context_budget = _context_budget(args.max_context_tokens, claude_backend)
            budget_note = ""
            if context_budget:
                budget_note = BUDGET_NOTE.format(
                    soft=context_budget - args.context_grace,
                    hard=context_budget)
            resume_id = None
            pi_session = None
            pi_resume = False
            # A recorded session id belongs to the CLI that minted it: a claude
            # id means nothing to pi and vice versa. Only a session tagged with
            # the backend about to run is warm-resumable; an untagged legacy
            # entry cold-starts once and is re-recorded with its tag.
            tagged = (current.get("session_backend")
                      if current.get("status") == "paused" else None)
            if claude_backend:
                if context_budget and tagged == "claude":
                    resume_id = current.get("session")
            else:
                # Pi warm resume: a pause re-enters the recorded per-plan session
                # (the harness resolves it to a cwd-independent `--session <file>`)
                # only while its session file exists; anything else — fresh plan,
                # failed retry, stale/foreign/untagged id — cold-starts a new id.
                recorded = current.get("session") if tagged == "pi" else None
                if recorded and _pi_session_exists(hd, recorded):
                    pi_session, pi_resume = recorded, True
                else:
                    pi_session = str(uuid.uuid4())
            template = RESUME_PROMPT if (resume_id or pi_resume) else RESOLVER_PROMPT
            prompt = template.format(
                plan_path=plan_path, goal_path=hd / "goal.md", ckpt=ckpt,
                budget_note=budget_note,
                checkpoint_max=args.checkpoint_chars, py=sys.executable,
                status_script=STATUS_SCRIPT, plan_file=e["file"], handover_dir=hd)
            state_path = hd / "logs" / f"{Path(e['file']).stem}.state.json"
            state_path.unlink(missing_ok=True)  # never re-read a previous round's state
            cmd = [str(args.harness), "-m", model, "--backend", backend]
            effort = _effective_effort(e.get("effort") or args.effort,
                                       e.get("status", "pending"), model)
            cmd += ["-t", effort]
            cmd += ["-s", str(args.stopwatch), "--grace", str(args.grace),
                    "--max-context-tokens", str(context_budget),
                    "--context-grace", str(args.context_grace)]
            if claude_backend:
                cmd += ["--state-file", str(state_path)]
            if resume_id:
                cmd += ["--resume-session", resume_id]
            if pi_session:
                (hd / "pi-sessions").mkdir(exist_ok=True)
                cmd += ["--pi-session-id", pi_session,
                        "--pi-session-dir", str(hd / "pi-sessions")]
            cmd += ["--", prompt]

            log_path = hd / "logs" / f"{Path(e['file']).stem}.log"
            fh = open(log_path, "a")  # append: keep earlier rounds' output for debugging
            fh.write(f"# {e['file']} | wave {wave} | stopwatch {args.stopwatch}s | "
                     f"backend {backend}"
                     f"{'' if backend != 'auto' else ' -> ' + ('claude' if claude_backend else 'pi')}"
                     f" | model {model} | ctx {context_budget} | "
                     f"resume {resume_id or '-'} | "
                     f"started {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            fh.flush()
            # CASPER_PY points the harness's guard route at this same venv python.
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, "CASPER_PY": sys.executable})
            running.append((e, proc, fh, log_path, effort, state_path,
                            bool(resume_id) or pi_resume, claude_backend,
                            context_budget, pi_session))

        for (e, proc, fh, log_path, effort, state_path, resumed,
             claude_backend, context_budget, pi_session) in running:
            code = proc.wait()
            fh.close()
            needs_user = _compact_checkpoint(hd / e["file"], args.checkpoint_chars)
            current = cs._find(cs._load_raw(hd), e["file"])
            already_done = bool(current) and current.get("status") == "done"
            status = _status_for(code, already_done, needs_user)
            session, pause_reason, zero_progress = None, None, 0
            if (status == "failed" and pi_session
                    and _pi_provider_limit(hd, pi_session)):
                # A transient provider usage/rate limit ended the run mid-flight.
                # "failed" would cold-restart (discarding the intact session) and
                # escalate effort; pause instead so the next round warm-resumes.
                status = "paused"
                pause_reason = "provider-limit"
                with open(log_path, "a") as lf:
                    lf.write("[fanout] provider usage/rate limit ended the run; "
                             "recording pause (warm resume) instead of failure\n")
            if status == "paused" and claude_backend:
                state = _read_state(state_path)
                session, pause_reason = _warm_session(
                    state, context_budget, args.context_grace)
                if state.get("first_call_over_budget"):
                    zero_progress = int((current or {}).get("zero_progress") or 0) + 1
                    if zero_progress >= _ZERO_PROGRESS_LIMIT:
                        _flag_over_budget(hd / e["file"], zero_progress,
                                          context_budget, args.checkpoint_chars)
                        needs_user = True  # blocks redispatch until the user resolves it
            elif (status == "paused" and pi_session
                    and _pi_session_exists(hd, pi_session)):
                session = pi_session  # next dispatch re-enters this pi session
            # Tag the id with the CLI that minted it, so the next dispatch only
            # resumes it on that same backend (and clears the tag with the id).
            session_backend = ("claude" if claude_backend else "pi") if session else None
            cs.set_status(hd, e["file"], status, session=session,
                          pause_reason=pause_reason, zero_progress=zero_progress,
                          session_backend=session_backend)
            results.append({"file": e["file"], "wave": wave, "exit_code": code,
                            "status": status, "needs_user": needs_user,
                            "effort": effort, "resumed": resumed,
                            "log": str(log_path)})

        # Gate later waves from the authoritative post-run ledger. This includes
        # plans completed by another fan-out while our batch was running.
        ledger = cs._load_raw(hd)
        wave_unfinished = [e for e in ledger if int(e.get("wave", 0)) == wave
                           and e.get("status", "pending") != "done"]
        if wave != waves[-1] and wave_unfinished:
            for e in selected:
                w = int(e.get("wave", 0))
                if w > wave and e.get("status", "pending") != "done":
                    results.append({"file": e["file"], "wave": w, "exit_code": None,
                                    "status": "skipped_dependency", "log": None})
            print(f"wave {wave} has unfinished plan(s) — skipping later wave(s); "
                  f"re-run to resume once it is done", file=sys.stderr)
            break

    if results:
        out = hd / "fanout-result.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(results, indent=2) + "\n")
        os.replace(tmp, out)  # atomic within the same directory
    done = sum(r["status"] == "done" for r in results)
    skipped = sum(r["status"] == "skipped_dependency" for r in results)
    summary = f"dispatched {len(results) - skipped} plan(s): {done} done, " \
              f"{len(results) - done - skipped} paused/failed/claimed"
    if skipped:
        summary += f", {skipped} skipped (dependency)"
    if results:
        summary += f" -> {hd / 'fanout-result.json'}"
    print(summary)

    final_ledger = cs._load_raw(hd)
    in_scope_files = {e["file"] for e in selected}
    all_done = all(e.get("status", "pending") == "done"
                   for e in final_ledger if e["file"] in in_scope_files)
    return 0 if all_done else 1


if __name__ == "__main__":
    raise SystemExit(main())
