"""Regression tests: casper_pi_guard must fail fast on a REJECTED rpc prompt.

pi's rpc layer answers a prompt it cannot start (failed preflight — missing or
bad credentials, unusable model) with a single
`{"type":"response","command":"prompt","success":false,"error":...}` line and
then stays silent forever (installed pi `dist/modes/rpc/rpc-mode.js`:
`error = (id, command, message) => ({id, type:"response", command,
success:false, error:message})` and the `session.prompt(...).catch(...)` guard).
The guard used to count that line as liveness and idle until the stopwatch —
exit 124, which fanout records as *paused* and redispatches forever.

Everything here runs against a scripted stand-in for `pi --mode rpc` in a temp
dir: no pi, no provider, no network, no auth file is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WF = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WF))

import casper_pi_guard as pi_guard

PYTHON = sys.executable

# Scripted stand-in for `pi --mode rpc`, private to this module (the shared
# FAKE_RPC in test_casper_helpers.py is owned elsewhere and must not change).
# argv: <scenario> [pidfile]
FAKE_REJECT_RPC = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, threading, time

scenario = sys.argv[1]
pidfile = sys.argv[2] if len(sys.argv) > 2 else None

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

got_abort = threading.Event()

def read_stdin():
    for line in sys.stdin:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if c.get("type") == "abort":
            got_abort.set()

threading.Thread(target=read_stdin, daemon=True).start()

AUTH_ERROR = ('Authentication failed for "anthropic". '
              "Run '/login anthropic' to authenticate.")
REJECT = {"type": "response", "command": "prompt", "success": False,
          "error": AUTH_ERROR}

def record(*pids):
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write("\n".join(str(p) for p in (os.getpid(), *pids)))

def spawn_grandchild():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid

def final(text, stop="stop"):
    emit({"type": "message_end", "message": {"role": "assistant",
          "content": [{"type": "text", "text": text}], "stopReason": stop}})
    emit({"type": "agent_end"})
    emit({"type": "agent_settled"})
    time.sleep(0.5)

def idle_forever():
    while True:
        time.sleep(1)

if scenario == "reject_silent":
    record(spawn_grandchild())
    time.sleep(0.2)
    emit(REJECT)
    idle_forever()
elif scenario == "reject_stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record()
    time.sleep(0.2)
    emit(REJECT)
    idle_forever()
elif scenario == "reject_verbose":
    ev = dict(REJECT)
    ev["error"] = ("line one\n" + "x" * 4000 + "\nsecret-looking tail")
    emit(ev)
    idle_forever()
elif scenario.startswith("reject_malformed"):
    ev = {"type": "response", "command": "prompt", "success": False}
    variant = scenario.split(":", 1)[1]
    if variant == "null":
        ev["error"] = None
    elif variant == "empty":
        ev["error"] = "   "
    elif variant == "object":
        ev["error"] = {"code": 401, "detail": ["nope"]}
    emit(ev)
    idle_forever()
elif scenario == "accepted":
    emit({"type": "response", "command": "prompt", "success": True})
    emit({"type": "agent_start"})
    final("FINAL ANSWER")
elif scenario == "unrelated_failures":
    emit({"type": "response", "command": "prompt", "success": True})
    emit({"type": "response", "command": "steer", "success": False,
          "error": "no active turn"})
    emit({"type": "response", "command": "get_state", "success": False,
          "error": "nope"})
    emit({"type": "response", "success": False, "error": "no command field"})
    emit({"type": "response", "command": "prompt"})
    final("FINAL ANSWER")
elif scenario == "late_reject":
    emit({"type": "response", "command": "prompt", "success": True})
    while not got_abort.is_set():
        emit({"type": "turn_start"}); time.sleep(0.3)
    emit(REJECT)                     # rejection arrives AFTER the abort
    final("ABORTED")
elif scenario == "accepted_then_silent":
    emit({"type": "response", "command": "prompt", "success": True})
    idle_forever()
'''


class PiRpcRejectTests(unittest.TestCase):
    """Rejected initial prompt -> prompt exit 1; everything else unchanged."""

    GUARD = WF / "casper_pi_guard.py"

    def _run(self, scenario: str, guard_args: list[str], timeout: int = 60,
             want_pids: bool = False):
        """Run the guard over the fake rpc child; return (proc, elapsed, pids)."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_reject_rpc.py"
            fake.write_text(FAKE_REJECT_RPC)
            pidfile = Path(tmp) / "pids"
            child = [PYTHON, str(fake), scenario]
            if want_pids:
                child.append(str(pidfile))
            started = time.monotonic()
            proc = subprocess.run(
                [PYTHON, str(self.GUARD), "--session-root", tmp,
                 "--rpc-prompt", "resolve the plan", *guard_args, "--", *child],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=timeout,
            )
            elapsed = time.monotonic() - started
            pids: list[int] = []
            if want_pids and pidfile.exists():
                pids = [int(p) for p in pidfile.read_text().split()]
            return proc, elapsed, pids

    # --- the bug -----------------------------------------------------------

    def test_rejected_prompt_then_silence_exits_one_fast_and_cleanly(self) -> None:
        # Baseline behavior: the rejection is treated as liveness and the run
        # idles to the stopwatch -> 124 after 25s. Fixed: exit 1 in ~1s.
        proc, elapsed, pids = self._run(
            "reject_silent",
            ["--stopwatch", "25", "--stall-secs", "60", "--poll-secs", "1"],
            timeout=60, want_pids=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)      # -> fanout `failed`
        self.assertLess(elapsed, 15, "rejection must not wait out the stopwatch")
        self.assertEqual(proc.stdout, "")                      # no final message exists
        self.assertIn("[pi-guard] pi rejected the prompt:", proc.stderr)
        self.assertIn("Authentication failed", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertLessEqual(len(proc.stderr.strip().splitlines()), 3, proc.stderr)
        self.assertEqual(len(pids), 2, "fake child + grandchild pids expected")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(pi_guard.alive(p) for p in pids):
            time.sleep(0.2)
        self.assertFalse([p for p in pids if pi_guard.alive(p)],
                         "no process of the rpc tree may survive the guard")

    def test_stubborn_child_is_killed_inside_the_cleanup_budget(self) -> None:
        proc, elapsed, pids = self._run(
            "reject_stubborn",
            ["--stopwatch", "60", "--stall-secs", "60", "--poll-secs", "1"],
            timeout=60, want_pids=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertLess(elapsed, 20, "SIGTERM-ignoring child must still be KILLed")
        self.assertIn("[pi-guard] pi rejected the prompt:", proc.stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(pi_guard.alive(p) for p in pids):
            time.sleep(0.2)
        self.assertFalse([p for p in pids if pi_guard.alive(p)])

    def test_rejection_diagnostic_is_one_bounded_stderr_line(self) -> None:
        proc, _, _ = self._run(
            "reject_verbose",
            ["--stopwatch", "25", "--stall-secs", "60", "--poll-secs", "1"],
            timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        lines = [ln for ln in proc.stderr.splitlines()
                 if "pi rejected the prompt:" in ln]
        self.assertEqual(len(lines), 1, proc.stderr)
        self.assertLess(len(lines[0]), 600, "diagnostic must stay bounded")
        self.assertIn("line one", lines[0])
        self.assertNotIn("secret-looking tail", lines[0])  # truncated, not dumped

    def test_malformed_error_payload_does_not_raise(self) -> None:
        for variant in ("missing", "null", "empty", "object"):
            with self.subTest(variant=variant):
                proc, _, _ = self._run(
                    f"reject_malformed:{variant}",
                    ["--stopwatch", "25", "--stall-secs", "60", "--poll-secs", "1"],
                    timeout=60)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("[pi-guard] pi rejected the prompt:", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")

    # --- everything the fix must NOT change --------------------------------

    def test_accepted_prompt_response_is_not_fatal(self) -> None:
        proc, _, _ = self._run(
            "accepted",
            ["--stopwatch", "30", "--stall-secs", "20", "--poll-secs", "1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "FINAL ANSWER\n")
        self.assertEqual(proc.stderr, "")  # happy path stays byte-silent

    def test_unrelated_failed_responses_are_not_fatal(self) -> None:
        proc, _, _ = self._run(
            "unrelated_failures",
            ["--stopwatch", "30", "--stall-secs", "20", "--poll-secs", "1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "FINAL ANSWER\n")
        self.assertNotIn("rejected the prompt", proc.stderr)

    def test_rejection_after_abort_is_still_a_budget_pause(self) -> None:
        proc, _, _ = self._run(
            "late_reject",
            ["--stopwatch", "2", "--stall-secs", "60", "--poll-secs", "1"],
            timeout=60)
        self.assertEqual(proc.returncode, 124, proc.stderr)  # -> fanout `paused`
        self.assertIn("abort sent", proc.stderr)
        self.assertNotIn("rejected the prompt", proc.stderr)

    def test_accepted_prompt_that_goes_silent_still_pauses(self) -> None:
        proc, _, _ = self._run(
            "accepted_then_silent",
            ["--stopwatch", "60", "--stall-secs", "2", "--grace", "1",
             "--poll-secs", "1"],
            timeout=60)
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertIn("no activity", proc.stderr)


if __name__ == "__main__":
    unittest.main()
