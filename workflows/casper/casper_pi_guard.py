#!/usr/bin/env python3
"""casper_pi_guard.py — liveness guard and rpc supervisor for Pi-routed calls.

Two modes over the command after `--`:

Default (process-tree) mode wraps one `pi -p` invocation. `pi -p` buffers its
stdout until exit and has no client-side timeout, so a hung streaming call
(e.g. a stalled ChatGPT-backend connection inside a pi subagent) is invisible
in the plan log and silently burns the whole wall-clock stopwatch. This guard
is the Pi-route sibling of casper_guard.py: it owns the stopwatch and watches
the child process tree for signs of life.

RPC mode (--rpc-prompt) wraps `pi --mode rpc ...` instead: the prompt is sent
as an rpc command, the JSONL event stream on the child's stdout is itself a
liveness signal (the /proc and session-file checks below remain as fallback for
quiet tool calls), a wind-down notice is steered in --wind-down-secs before the
stopwatch (delivered at the next turn boundary; self-contained, mirrors
casper_guard's notice), the stopwatch sends a clean `abort` (10s to settle,
then the tree is killed — a child that never spoke rpc is killed immediately),
extension UI dialogs are answered `cancelled` (matching `pi -p`'s headless
"blocks outright" behavior, and avoiding hangs on timeout-less dialogs), a
prompt the rpc layer *rejects* (failed preflight, e.g. missing credentials)
fails fast instead of idling to the stopwatch, and stdout becomes the FINAL
assistant message text (what casper_verify.py wants; raw events would be
noise). Exit: 0 settled, 1 settled with stopReason "error" or a rejected
prompt, 124 for every guard stop, child's code if it dies before settling.

Liveness signals, polled every --poll-secs:
  - CPU-time growth across the child's process tree (/proc/<pid>/stat),
  - io-counter growth across the tree (/proc/<pid>/io rchar+wchar — network
    reads from a streaming response count here),
  - new writes to pi session files (~/.pi/agent/{sessions,subagent-sessions})
    whose path contains the working directory's basename.

After --stall-secs with none of these, the guard first SIGTERMs wedged *leaf*
`pi` subagent processes (never the root agent): the parent sees its subagent
die, treats it as a failed tool call, and usually resumes within seconds. If
the tree stays silent for --grace more seconds (or no leaf exists, or
--max-unsticks is exhausted), the guard kills the whole run and exits 124 so
fanout records a *pause* — a stall must never mark the plan failed or
escalate effort. The stopwatch elapsing likewise kills the run with exit 124.

In default mode the child inherits stdout/stderr: stdout stays byte-for-byte
the model's output. Guard messages go to stderr prefixed "[pi-guard]" and land
in the plan log via fanout; the happy path prints nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

TICK = os.sysconf("SC_CLK_TCK")
CPU_ACTIVE_SECS = 2.0        # tree CPU growth per window that counts as alive
IO_ACTIVE_BYTES = 1 << 20    # tree rchar+wchar growth per window that counts as alive


def log(msg: str) -> None:
    print(f"[pi-guard] {msg}", file=sys.stderr, flush=True)


def _read_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the (comm) — comm may contain spaces."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def tree_pids(root: int) -> list[int]:
    """root plus all its descendants, from a single /proc scan."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        parts = _read_stat(int(entry))
        if parts is None or len(parts) < 2:
            continue
        try:
            children.setdefault(int(parts[1]), []).append(int(entry))
        except ValueError:
            continue
    out: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def tree_stats(pids: list[int]) -> tuple[float, int]:
    """(total CPU seconds, total rchar+wchar bytes) across pids."""
    cpu = 0.0
    io = 0
    for pid in pids:
        parts = _read_stat(pid)
        if parts is not None and len(parts) > 12:
            try:
                cpu += (int(parts[11]) + int(parts[12])) / TICK  # utime+stime
            except ValueError:
                pass
        try:
            with open(f"/proc/{pid}/io") as f:
                for line in f:
                    if line.startswith(("rchar:", "wchar:")):
                        io += int(line.split()[1])
        except (OSError, ValueError):
            pass
    return cpu, io


def newest_session_mtime(session_root: Path, needle: str,
                         watch_dirs: tuple[Path, ...] = ()) -> float:
    """Newest *.jsonl mtime for THIS run: the dedicated watch dirs when given,
    otherwise the needle-filtered shared agent trees.

    A watch dir (a fanout --pi-session-dir) is dedicated to this resolver, so it
    is the only trustworthy activity signal and must not be unioned with the
    shared trees. The needle is only `basename(cwd)` -- the worktree name -- so
    the shared trees also match every unrelated session sharing that basename
    (the driving session, its subagents). Unioning them let those writes reset
    the stall timer continuously and blinded the watchdog: a wedged resolver was
    never TERMed at --stall-secs and instead burned its whole --stopwatch.
    The shared-tree scan therefore stays a fallback for standalone runs only.
    """
    latest = 0.0
    if watch_dirs:
        scans = [(Path(d), "") for d in watch_dirs]
    else:
        scans = [(session_root / sub, needle)
                 for sub in ("sessions", "subagent-sessions")]
    for base, filt in scans:
        if not base.is_dir():
            continue
        for f in base.rglob("*.jsonl"):
            if filt and filt not in str(f):
                continue
            try:
                latest = max(latest, f.stat().st_mtime)
            except OSError:
                continue
    return latest


def alive(pid: int) -> bool:
    """False for exited AND zombie processes (an unreaped child still answers
    os.kill(pid, 0), so signal-probing would stall the post-TERM wait loop)."""
    parts = _read_stat(pid)
    return parts is not None and parts[0] != "Z"


def term_then_kill(pids: list[int], wait_secs: float = 10.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if not any(alive(p) for p in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def wedged_pi_leaves(root: int) -> list[int]:
    """Leaf `pi` processes in root's tree, excluding root itself."""
    pids = tree_pids(root)
    pi_pids = {p for p in pids if _comm(p) == "pi"}
    parents: dict[int, int] = {}
    for pid in pids:
        parts = _read_stat(pid)
        if parts is not None and len(parts) >= 2:
            try:
                parents[pid] = int(parts[1])
            except ValueError:
                continue
    have_pi_child = {parents[p] for p in pi_pids if p in parents}
    return sorted(p for p in pi_pids if p != root and p not in have_pi_child)


_ABORT_GRACE = 10.0  # seconds a live rpc agent gets to settle after `abort`
_REJECT_GRACE = 5.0  # seconds a rejected-prompt child gets to exit on SIGTERM
_REJECT_DETAIL_MAX = 500  # stderr bound for pi's rejection message


def _rpc_send(child: subprocess.Popen, obj: dict) -> bool:
    """One JSONL command to the rpc child; False when its stdin is gone."""
    try:
        child.stdin.write(json.dumps(obj) + "\n")
        child.stdin.flush()
        return True
    except (OSError, ValueError):
        return False


def _reject_detail(ev: dict) -> str:
    """One bounded stderr line from a failed rpc response payload.

    The payload is whatever pi sent, so it may be missing, empty or not even a
    string; it is never inspected for meaning, only collapsed to a single line
    and truncated so a verbose provider error cannot flood the plan log.
    """
    detail = ev.get("error")
    if not isinstance(detail, str):
        detail = "" if detail is None else repr(detail)
    detail = " ".join(detail.split())
    if not detail:
        return "no error detail in the rpc response"
    if len(detail) > _REJECT_DETAIL_MAX:
        return detail[:_REJECT_DETAIL_MAX] + "..."
    return detail


def _assistant_text(message: dict) -> str:
    return "".join(c.get("text", "") for c in (message.get("content") or [])
                   if isinstance(c, dict) and c.get("type") == "text")


def rpc_supervise(args: argparse.Namespace, cmd: list[str]) -> int:
    """Drive one `pi --mode rpc` run: prompt, supervise, print the final text."""
    try:
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
    except OSError as exc:
        log(f"cannot spawn child: {exc}")
        return 2

    events: queue.Queue = queue.Queue()

    def _reader() -> None:
        for line in child.stdout:
            events.put(line)
        events.put(None)  # EOF sentinel

    threading.Thread(target=_reader, daemon=True).start()

    def kill_run(reason: str) -> None:
        log(f"{reason}; killing pi process tree (exit 124 -> pause)")
        term_then_kill(tree_pids(child.pid))
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def on_term(signum: int, frame: object) -> None:
        kill_run(f"received signal {signum}")
        os._exit(124)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    _rpc_send(child, {"type": "prompt", "message": args.rpc_prompt})

    deadline = time.monotonic() + args.stopwatch
    # A wind-down window that does not fit inside the stopwatch means there is
    # no wind-down phase at all — steering the notice at t=0 would just pollute
    # the conversation before any work happened.
    wind_at = (deadline - args.wind_down_secs
               if 0 < args.wind_down_secs < args.stopwatch else None)
    needle = os.path.basename(os.getcwd())
    watch_dirs = tuple(args.watch_dir)
    last_cpu, last_io = tree_stats(tree_pids(child.pid))
    last_sess = newest_session_mtime(args.session_root, needle, watch_dirs)
    last_active = time.monotonic()
    unsticks = 0
    saw_event = False
    wound_down = False
    aborted_at: float | None = None
    settled = False
    final_text, stop_reason = "", None

    def finish(code: int) -> int:
        if final_text:
            print(final_text, flush=True)
        if child.poll() is None:
            try:
                child.stdin.close()
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                term_then_kill(tree_pids(child.pid))
        return code

    while True:
        now = time.monotonic()
        step = min(args.poll_secs, max(deadline - now, 0.1))
        if wind_at is not None and not wound_down:
            step = min(step, max(wind_at - now, 0.1))
        try:
            item = events.get(timeout=step)
        except queue.Empty:
            item = ""
        if item is None:  # EOF: the child died before settling
            rc = child.wait()
            code = rc if rc >= 0 else 128 - rc
            if aborted_at is not None:
                code = 124  # the abort caused this exit: a budget stop, not a fail
            return finish(code)  # 0-without-settle maps to "paused" in fanout
        if item:
            saw_event = True
            last_active = time.monotonic()
            try:
                ev = json.loads(item)
            except ValueError:
                ev = None
            if isinstance(ev, dict):
                kind = ev.get("type")
                if (kind == "extension_ui_request" and ev.get("id")
                        and ev.get("method") != "notify"):
                    # Match `pi -p` headless behavior (dialogs block outright)
                    # and never hang on a timeout-less dialog.
                    _rpc_send(child, {"type": "extension_ui_response",
                                      "id": ev["id"], "cancelled": True})
                    log(f"cancelled extension dialog: {ev.get('title') or ev.get('method')}")
                elif kind == "message_end":
                    message = ev.get("message") or {}
                    if message.get("role") == "assistant":
                        text = _assistant_text(message)
                        if text.strip():
                            final_text = text
                        stop_reason = message.get("stopReason") or stop_reason
                elif kind == "agent_settled":
                    settled = True
                elif (kind == "response" and ev.get("command") == "prompt"
                        and ev.get("success") is False and aborted_at is None):
                    # pi's rpc layer answers a rejected prompt (failed
                    # preflight: bad/missing credentials, unusable model) with
                    # one failure response and then stays silent forever. That
                    # line is not liveness: nothing else will ever arrive, so
                    # idling would burn the whole stopwatch and report a *pause*
                    # that fanout redispatches. Scope: only the prompt command
                    # (a failed steer/abort keeps its own path) and only before
                    # an abort (a late rejection during wind-down is still a
                    # budget stop -> 124).
                    log(f"pi rejected the prompt: {_reject_detail(ev)}")
                    term_then_kill(tree_pids(child.pid), wait_secs=_REJECT_GRACE)
                    return finish(1)  # 1 -> fanout records `failed`, not paused

        now = time.monotonic()
        if settled:
            if aborted_at is not None:
                log("abort honored; run settled")
                return finish(124)
            return finish(1 if stop_reason == "error" else 0)
        if aborted_at is not None:
            if now - aborted_at > _ABORT_GRACE:
                kill_run(f"abort not honored within {int(_ABORT_GRACE)}s")
                return finish(124)
            continue  # draining until it settles or the grace runs out
        if now >= deadline:
            if saw_event and _rpc_send(child, {"type": "abort"}):
                log(f"stopwatch ({args.stopwatch}s) elapsed; abort sent")
                aborted_at = now
            else:  # never spoke rpc (or stdin gone): nothing to wind down
                kill_run(f"stopwatch ({args.stopwatch}s) elapsed")
                return finish(124)
            continue
        if wind_at is not None and not wound_down and now >= wind_at:
            wound_down = True
            import casper_guard  # sibling; single source of the notice wording
            notice = casper_guard.wind_down_text("time", 0, 0, deadline - now)
            if _rpc_send(child, {"type": "steer", "message": notice}):
                log(f"wind-down notice steered ({int(deadline - now)}s left)")

        if item:
            continue  # events are liveness; only probe /proc when the stream is quiet
        pids = tree_pids(child.pid)
        cpu, io = tree_stats(pids)
        sess = newest_session_mtime(args.session_root, needle, watch_dirs)
        if (cpu - last_cpu >= CPU_ACTIVE_SECS
                or io - last_io >= IO_ACTIVE_BYTES
                or sess > last_sess):
            last_active = now
        last_cpu = max(last_cpu, cpu) if cpu >= last_cpu else cpu
        last_io = max(last_io, io) if io >= last_io else io
        last_sess = max(last_sess, sess)
        silent_for = now - last_active
        if silent_for <= args.stall_secs:
            continue
        leaves = wedged_pi_leaves(child.pid)
        if leaves and unsticks < args.max_unsticks:
            unsticks += 1
            log(f"no activity for {int(silent_for)}s; SIGTERM wedged pi "
                f"subagent leaf(s) {leaves} (unstick {unsticks}/{args.max_unsticks})")
            term_then_kill(leaves)
            last_active = time.monotonic() - args.stall_secs + args.grace
            continue
        kill_run(f"no activity for {int(silent_for)}s and no recoverable "
                 f"pi subagent leaf")
        return finish(124)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stall-secs", type=int, default=900)
    ap.add_argument("--grace", type=int, default=60)
    ap.add_argument("--stopwatch", type=int, default=7200)
    ap.add_argument("--poll-secs", type=float, default=15.0)
    ap.add_argument("--max-unsticks", type=int, default=3)
    ap.add_argument("--session-root", type=Path,
                    default=Path(os.environ.get("PI_CODING_AGENT_DIR")
                                 or Path.home() / ".pi" / "agent"),
                    help="pi agent dir whose sessions/subagent-sessions feed the "
                         "liveness check (default: $PI_CODING_AGENT_DIR, else "
                         "~/.pi/agent)")
    ap.add_argument("--watch-dir", type=Path, action="append", default=[],
                    help="extra directory whose *.jsonl writes count as liveness "
                         "(e.g. a fanout --pi-session-dir); repeatable")
    ap.add_argument("--rpc-prompt", default=None,
                    help="supervise the child as `pi --mode rpc` and send this "
                         "prompt (default: process-tree mode over `pi -p`)")
    ap.add_argument("--wind-down-secs", type=int, default=0,
                    help="rpc mode: steer the wind-down notice this many seconds "
                         "before the stopwatch (0 = off)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- pi -p ... (verbatim child command)")
    args = ap.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        log("no child command given")
        return 2
    if args.rpc_prompt is not None:
        return rpc_supervise(args, cmd)

    needle = os.path.basename(os.getcwd())
    child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def kill_run(reason: str) -> None:
        log(f"{reason}; killing pi process tree (exit 124 -> pause)")
        term_then_kill(tree_pids(child.pid))
        try:
            child.wait(timeout=5)  # reap; the tree is already dead or KILLed
        except subprocess.TimeoutExpired:
            pass

    def on_term(signum: int, frame: object) -> None:
        kill_run(f"received signal {signum}")
        os._exit(124)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    deadline = time.monotonic() + args.stopwatch
    last_cpu, last_io = tree_stats(tree_pids(child.pid))
    watch_dirs = tuple(args.watch_dir)
    last_sess = newest_session_mtime(args.session_root, needle, watch_dirs)
    last_active = time.monotonic()
    unsticks = 0

    while True:
        step = min(args.poll_secs, deadline - time.monotonic())
        if step > 0:
            try:
                rc = child.wait(timeout=step)
                return rc if rc >= 0 else 128 - rc
            except subprocess.TimeoutExpired:
                pass
        if time.monotonic() >= deadline:
            kill_run(f"stopwatch ({args.stopwatch}s) elapsed")
            return 124

        pids = tree_pids(child.pid)
        cpu, io = tree_stats(pids)
        sess = newest_session_mtime(args.session_root, needle, watch_dirs)
        if (cpu - last_cpu >= CPU_ACTIVE_SECS
                or io - last_io >= IO_ACTIVE_BYTES
                or sess > last_sess):
            last_active = time.monotonic()
        # Sums can shrink when a descendant exits; rebase without marking active
        # (the parent's resumption shows up as session/cpu/io growth on its own).
        last_cpu = max(last_cpu, cpu) if cpu >= last_cpu else cpu
        last_io = max(last_io, io) if io >= last_io else io
        last_sess = max(last_sess, sess)

        silent_for = time.monotonic() - last_active
        if silent_for <= args.stall_secs:
            continue

        leaves = wedged_pi_leaves(child.pid)
        if leaves and unsticks < args.max_unsticks:
            unsticks += 1
            log(f"no activity for {int(silent_for)}s; SIGTERM wedged pi "
                f"subagent leaf(s) {leaves} (unstick {unsticks}/{args.max_unsticks})")
            term_then_kill(leaves)
            # Re-arm so the next stall check fires after --grace, not a full
            # stall window; real activity resets last_active as usual.
            last_active = time.monotonic() - args.stall_secs + args.grace
            continue

        kill_run(f"no activity for {int(silent_for)}s and no recoverable "
                 f"pi subagent leaf")
        return 124


if __name__ == "__main__":
    sys.exit(main())
