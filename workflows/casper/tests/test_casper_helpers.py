from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

WF = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WF))

import casper_fanout as fanout
import casper_guard as guard
import casper_pi_guard as pi_guard
import casper_status as status
import casper_verify as verify

PYTHON = sys.executable

# The suite must behave identically under any driving harness: strip the pi
# driver env so the historical Claude default holds everywhere, and tests that
# cover the driver-aware default set these variables explicitly per subprocess.
for _var in ("PI_CODING_AGENT", "PI_PROVIDER", "PI_MODEL"):
    os.environ.pop(_var, None)

PI_DRIVER_ENV = {"PI_CODING_AGENT": "true", "PI_PROVIDER": "anthropic",
                 "PI_MODEL": "claude-opus-5"}


class StatusTests(unittest.TestCase):
    def test_claim_duration_cannot_be_shortened_by_another_caller(self) -> None:
        entry = {
            "status": "in_progress",
            "lease": {"started_at": 100.0, "stale_secs": 900},
        }
        self.assertFalse(status.is_open(entry, stale_secs=10, now=999.0))
        self.assertTrue(status.is_open(entry, stale_secs=10, now=1001.0))

    def test_session_survives_init_and_bare_pause_but_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp)
            (hd / "plan.md").write_text("---\nstatus: paused\n---\n")
            (hd / "plans.json").write_text(json.dumps([{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "session": "sess-1", "pause_reason": "child-exit", "zero_progress": 1}]))
            status.init(hd)
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual(entry["session"], "sess-1")
            self.assertEqual(entry["zero_progress"], 1)
            # a bare paused set (pre-dispatch NEEDS-USER path) preserves the session
            status.set_status(hd, "plan.md", "paused")
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual(entry["session"], "sess-1")
            self.assertEqual(entry["pause_reason"], "child-exit")
            self.assertEqual(entry["zero_progress"], 1)
            # any non-paused transition invalidates it
            status.set_status(hd, "plan.md", "done")
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertIsNone(entry["session"])
            self.assertIsNone(entry["pause_reason"])
            self.assertEqual(entry["zero_progress"], 0)

    def test_live_claim_is_unresolved_but_not_dispatchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp)
            (hd / "plan.md").write_text("---\nstatus: pending\n---\n")
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            self.assertEqual(status.claim(hd, "plan.md", 900), "claimed")
            self.assertEqual(status.list_open(hd, 10), [])
            self.assertEqual(status.list_unresolved(hd), ["plan.md"])

    def test_scaffold_creates_plan_ledger_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp) / "my-goal"
            hd.mkdir()
            with self.assertRaises(FileNotFoundError):
                status.scaffold(hd)  # only an approved goal (goal.md) gets a ledger
            (hd / "goal.md").write_text("## Goal\nx\n")
            res = status.scaffold(hd)
            self.assertEqual(res, {"plan": "plan-01-my-goal.md", "created_plan": True,
                                   "created_entry": True, "seeded": 1})
            plan = hd / "plan-01-my-goal.md"
            self.assertIn("status: pending", plan.read_text())
            self.assertIn("## Progress / Handover", plan.read_text())
            entry = status._find(status._load_raw(hd), "plan-01-my-goal.md")
            self.assertEqual((entry["status"], entry["wave"]), ("pending", 0))
            # a re-run keeps an edited plan doc and the live ledger status
            plan.write_text(plan.read_text().replace(
                "<complete the approved goal; include relevant paths, constraints, "
                "and focused checks>", "real objective"))
            status.set_status(hd, "plan-01-my-goal.md", "done")
            res = status.scaffold(hd)
            self.assertEqual((res["created_plan"], res["created_entry"]), (False, False))
            self.assertIn("real objective", plan.read_text())
            entry = status._find(status._load_raw(hd), "plan-01-my-goal.md")
            self.assertEqual(entry["status"], "done")

    def test_health_audits_completion_without_writing(self) -> None:
        code, lines = status.health(Path("/nonexistent/handover"))
        self.assertEqual(code, 1)
        self.assertIn("nothing to check", lines[0])
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp)
            (hd / "goal.md").write_text("## Goal\nx\n")
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"t","wave":0}]')
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n\n## Objective\nx\n\n## Progress / Handover\n")
            status.init(hd)
            code, lines = status.health(hd)
            report = "\n".join(lines)
            self.assertEqual(code, 1)  # unresolved plan + missing verify.json
            self.assertIn("plans: unresolved — plan.md (pending)", report)
            self.assertIn("no verify.json", report)
            status.set_status(hd, "plan.md", "done")
            (hd / "verify.json").write_text('[{"criterion":"c","status":"pass"}]')
            snapshot = {p: p.stat().st_mtime_ns for p in hd.iterdir()}
            code, lines = status.health(hd)
            self.assertEqual((code, lines[-1]), (0, "healthy"))
            self.assertEqual({p: p.stat().st_mtime_ns for p in hd.iterdir()}, snapshot)
            # an open NEEDS-USER line in the checkpoint always wins
            with (hd / "plan.md").open("a") as f:
                f.write("NEEDS-USER: which branch?\n")
            code, lines = status.health(hd)
            self.assertEqual(code, 1)
            self.assertIn("needs-user: open NEEDS-USER in plan.md", "\n".join(lines))


class FanoutTests(unittest.TestCase):
    def test_checkpoint_is_bounded_and_keeps_one_needs_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.md"
            plan.write_text(
                "## Objective\nDo it\n\n## Progress / Handover\n"
                "old\nNEEDS-USER: first\n" + ("x" * 300) + "\nNEEDS-USER: latest\n"
            )
            self.assertTrue(fanout._compact_checkpoint(plan, 100))
            text = plan.read_text()
            body = fanout._PROGRESS_RE.search(text).group(1).strip()
            self.assertLessEqual(len(body), 100)
            self.assertEqual(body.count("NEEDS-USER: "), 1)
            self.assertIn("NEEDS-USER: latest", body)

    def test_mem_available_is_parsed_and_missing_probe_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp) / "meminfo"
            meminfo.write_text(
                "MemTotal:       65679416 kB\n"
                "MemFree:         1048576 kB\n"
                "MemAvailable:    2097152 kB\n"
                "Buffers:          123456 kB\n"
            )
            self.assertAlmostEqual(fanout._mem_available_gb(str(meminfo)), 2.0)
            self.assertIsNone(fanout._mem_available_gb(str(Path(tmp) / "nope")))
            (Path(tmp) / "empty").write_text("MemTotal: 1 kB\n")
            self.assertIsNone(fanout._mem_available_gb(str(Path(tmp) / "empty")))

    def test_wait_for_host_is_bounded_and_skips_probe_when_disabled(self) -> None:
        sleeps: list[int] = []

        def sleeper(seconds: int) -> None:
            sleeps.append(seconds)

        # Green on the first probe: no waiting at all.
        self.assertTrue(fanout._wait_for_host(
            4.0, 900, poll=30, probe=lambda: 8.0, sleep=sleeper))
        self.assertEqual(sleeps, [])

        # Red then green: exactly one poll interval slept.
        readings = iter([1.0, 9.0])
        self.assertTrue(fanout._wait_for_host(
            4.0, 900, poll=30, probe=lambda: next(readings), sleep=sleeper))
        self.assertEqual(sleeps, [30])

        # Red for the whole window: bounded by max_wait / poll, then False.
        sleeps.clear()
        self.assertFalse(fanout._wait_for_host(
            4.0, 90, poll=30, probe=lambda: 0.5, sleep=sleeper))
        self.assertEqual(len(sleeps), 3)  # ceil(90 / 30)

        # An unreadable probe never blocks dispatch.
        sleeps.clear()
        self.assertTrue(fanout._wait_for_host(
            4.0, 90, poll=30, probe=lambda: None, sleep=sleeper))
        self.assertEqual(sleeps, [])

        # The gate is off at 0: the probe is never even called.
        def must_not_probe() -> float:
            raise AssertionError("probed while the host gate is disabled")

        self.assertTrue(fanout._wait_for_host(
            0, 900, poll=30, probe=must_not_probe, sleep=sleeper))
        self.assertEqual(sleeps, [])

    def test_host_gate_skips_plan_without_claiming_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            marker = root / "called"
            harness = root / "must_not_run"
            harness.write_text(f"#!/bin/sh\ntouch {marker}\n")
            harness.chmod(0o755)
            # An unreachable threshold makes the real probe red; --host-wait 0
            # returns immediately, so the gate is deterministic and instant.
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5",
                 "--min-free-gb", "999999", "--host-wait", "0"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertFalse(marker.exists())  # never dispatched
            results = json.loads((hd / "fanout-result.json").read_text())
            self.assertEqual([row["status"] for row in results], ["skipped_host_busy"])
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual(entry["status"], "pending")  # never claimed
            self.assertIsNone(entry.get("lease"))
            self.assertEqual(status.list_open(hd, 900), ["plan.md"])  # still dispatchable

    def test_needs_user_wins_over_resolver_done_signal(self) -> None:
        self.assertEqual(fanout._status_for(0, already_done=True, needs_user=True), "paused")

    def test_pi_provider_limit_detects_terminal_limit_errors_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp)
            (hd / "pi-sessions").mkdir()
            sid = "abc123"
            f = hd / "pi-sessions" / f"2026-01-01T00-00-00-000Z_{sid}.jsonl"

            def entry(role: str, stop: str | None = None,
                      err: str | None = None) -> str:
                msg: dict = {"role": role}
                if stop:
                    msg["stopReason"] = stop
                if err:
                    msg["errorMessage"] = err
                return json.dumps({"type": "message", "message": msg})

            # Terminal usage-limit / 429 errors -> True.
            f.write_text("\n".join([
                entry("assistant", "toolUse"),
                entry("toolResult"),
                entry("assistant", "error",
                      "Codex error: The usage limit has been reached"),
            ]) + "\n")
            self.assertTrue(fanout._pi_provider_limit(hd, sid))
            f.write_text(entry(
                "assistant", "error",
                '429 {"type":"error","error":{"type":"rate_limit_error"}}') + "\n")
            self.assertTrue(fanout._pi_provider_limit(hd, sid))

            # A recovered session (later successful assistant message) -> False.
            f.write_text("\n".join([
                entry("assistant", "error",
                      "Codex error: The usage limit has been reached"),
                entry("assistant", "stop"),
            ]) + "\n")
            self.assertFalse(fanout._pi_provider_limit(hd, sid))

            # Unrelated terminal error, missing session file -> False.
            f.write_text(entry("assistant", "error", "boom") + "\n")
            self.assertFalse(fanout._pi_provider_limit(hd, sid))
            self.assertFalse(fanout._pi_provider_limit(hd, "missing"))

    def test_model_aware_effort_defaults_cover_supported_forms(self) -> None:
        cases = {
            # Sol aliases and both bare/provider-qualified ids.
            "sol": "high",
            "GPT 5.6 Sol": "high",
            "gpt-5.6-sol": "high",
            "openai-codex/gpt-5.6-sol": "high",
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(
                    fanout._effective_effort(None, "pending", model), expected
                )

    def test_unrelated_models_fall_back_to_medium_and_overrides_are_preserved(self) -> None:
        for model in ("", "opus", "claude-opus-5", "anthropic/claude-opus-5",
                      "claude-sonnet-5", "gpt-5.5", "provider/custom-model"):
            with self.subTest(model=model):
                self.assertEqual(
                    fanout._effective_effort(None, "pending", model), "medium"
                )
        for configured in fanout._EFFORTS:
            with self.subTest(configured=configured):
                self.assertEqual(
                    fanout._effective_effort(configured.upper(), "pending", "opus"),
                    configured,
                )

    def test_fanout_escalates_exactly_one_level_only_after_failure(self) -> None:
        cases = (
            (None, "paused", "opus", "medium"),
            (None, "failed", "opus", "high"),
            (None, "failed", "gpt-5.6-sol", "xhigh"),
            (None, "failed", "gpt-5.5", "high"),
            ("high", "failed", "gpt-5.5", "xhigh"),
            ("max", "failed", "gpt-5.5", "max"),
        )
        for configured, status_name, model, expected in cases:
            with self.subTest(configured=configured, status=status_name, model=model):
                self.assertEqual(
                    fanout._effective_effort(configured, status_name, model), expected
                )

    def test_single_plan_happy_path_uses_one_harness_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            count = root / "count"
            harness = root / "fake_harness.py"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib,re,subprocess,sys\n"
                f"p=pathlib.Path({str(count)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
                "prompt=sys.argv[-1]\n"
                "m=re.search(r'\\\"([^\\\"]+)\\\" \\\"([^\\\"]+)\\\" --set \\\"([^\\\"]+)\\\" done --handover-dir \\\"([^\\\"]+)\\\"', prompt)\n"
                "subprocess.run([m.group(1),m.group(2),'--set',m.group(3),'done','--handover-dir',m.group(4)],check=True)\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5", "--grace", "1", "--slack", "1"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertEqual(count.read_text(), "1")
            self.assertEqual(status.list_unresolved(hd), [])

    def test_model_precedence_is_plan_then_cli_then_opus(self) -> None:
        def capture(plan_model: str | None, cli_model: str | None) -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                hd = root / "handover"
                hd.mkdir()
                (hd / "goal.md").write_text("## Goal\nDo it\n")
                (hd / "plan.md").write_text(
                    "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
                )
                entry = {"file": "plan.md", "title": "one", "wave": 0}
                if plan_model is not None:
                    entry["model"] = plan_model
                (hd / "plans.json").write_text(json.dumps([entry]))
                status.init(hd)
                argv_dump = root / "argv.json"
                harness = root / "fake_harness.py"
                harness.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json,pathlib,re,subprocess,sys\n"
                    f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))\n"
                    "prompt=sys.argv[-1]\n"
                    "m=re.search(r'\\\"([^\\\"]+)\\\" \\\"([^\\\"]+)\\\" --set \\\"([^\\\"]+)\\\" done --handover-dir \\\"([^\\\"]+)\\\"', prompt)\n"
                    "subprocess.run([m.group(1),m.group(2),'--set',m.group(3),'done','--handover-dir',m.group(4)],check=True)\n"
                )
                harness.chmod(0o755)
                cmd = [PYTHON, str(WF / "casper_fanout.py"),
                       "--handover-dir", str(hd), "--harness", str(harness),
                       "--stopwatch", "5", "--grace", "1", "--slack", "1"]
                if cli_model is not None:
                    cmd += ["--model", cli_model]
                run = subprocess.run(
                    cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout)
                return json.loads(argv_dump.read_text())

        cases = (
            # plan model wins and drives the model-aware effort default
            ("openai-codex/gpt-5.6-sol", "gpt-5.5",
             "openai-codex/gpt-5.6-sol", "high"),
            (None, "gpt-5.6-sol", "gpt-5.6-sol", "high"),
            (None, None, "opus", "medium"),
        )
        for plan_model, cli_model, expected_model, expected_effort in cases:
            with self.subTest(plan_model=plan_model, cli_model=cli_model):
                argv = capture(plan_model, cli_model)
                self.assertEqual(argv[argv.index("-m") + 1], expected_model)
                self.assertEqual(argv[argv.index("-t") + 1], expected_effort)

    def test_fanout_threads_token_budget_flags_to_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            argv_dump = root / "argv.json"
            harness = root / "fake_harness.py"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,re,subprocess,sys\n"
                f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))\n"
                "prompt=sys.argv[-1]\n"
                "m=re.search(r'\\\"([^\\\"]+)\\\" \\\"([^\\\"]+)\\\" --set \\\"([^\\\"]+)\\\" done --handover-dir \\\"([^\\\"]+)\\\"', prompt)\n"
                "subprocess.run([m.group(1),m.group(2),'--set',m.group(3),'done','--handover-dir',m.group(4)],check=True)\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5", "--grace", "1", "--slack", "1"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            argv = json.loads(argv_dump.read_text())
            self.assertEqual(argv[argv.index("--max-context-tokens") + 1], "470000")
            self.assertEqual(argv[argv.index("--context-grace") + 1], "40000")
            self.assertIn("context budget", argv[-1])  # RESOLVER_PROMPT budget note

    def test_fanout_warm_resumes_paused_plan_with_recorded_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: paused\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            # session_backend tags the id with the CLI that minted it; only a
            # matching backend warm-resumes it (an untagged legacy id restarts).
            (hd / "plans.json").write_text(json.dumps([{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "session": "sess-warm-1", "session_backend": "claude",
                "pause_reason": "child-exit"}]))
            status.init(hd)
            argv_dump = root / "argv.json"
            harness = root / "fake_harness.py"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,re,subprocess,sys\n"
                f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))\n"
                "prompt=sys.argv[-1]\n"
                "m=re.search(r'\\\"([^\\\"]+)\\\" \\\"([^\\\"]+)\\\" --set \\\"([^\\\"]+)\\\" done --handover-dir \\\"([^\\\"]+)\\\"', prompt)\n"
                "subprocess.run([m.group(1),m.group(2),'--set',m.group(3),'done','--handover-dir',m.group(4)],check=True)\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5", "--grace", "1", "--slack", "1"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            argv = json.loads(argv_dump.read_text())
            self.assertEqual(argv[argv.index("--resume-session") + 1], "sess-warm-1")
            self.assertIn("RESUMING", argv[-1])  # short resume prompt, not RESOLVER_PROMPT
            self.assertTrue(json.loads((hd / "fanout-result.json").read_text())[0]["resumed"])
            # completing the plan invalidates the recorded session
            self.assertIsNone(status._find(status._load_raw(hd), "plan.md")["session"])

    def test_fanout_pi_backend_mints_and_resumes_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text(json.dumps([
                {"file": "plan.md", "title": "one", "wave": 0,
                 "model": "prov/model-x"}]))
            status.init(hd)
            argv_dir = root / "argv"
            argv_dir.mkdir()
            mode = root / "mode"
            mode.write_text("pause")
            harness = root / "fake_harness.py"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,sys,time\n"
                f"d=pathlib.Path({str(argv_dir)!r})\n"
                "d.joinpath(f'{time.monotonic_ns()}.json').write_text(json.dumps(sys.argv))\n"
                "# a real pi run writes its session file; recording is gated on it\n"
                "sid=sys.argv[sys.argv.index('--pi-session-id')+1]\n"
                "sd=pathlib.Path(sys.argv[sys.argv.index('--pi-session-dir')+1])\n"
                "sd.mkdir(exist_ok=True)\n"
                "(sd/f'2026-01-01T00-00-00-000Z_{sid}.jsonl').touch()\n"
                f"sys.exit(3 if pathlib.Path({str(mode)!r}).read_text().strip()=='fail' else 0)\n"
            )
            harness.chmod(0o755)

            def run_fanout() -> list[str]:
                run = subprocess.run(
                    [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                     "--harness", str(harness), "--stopwatch", "5",
                     "--grace", "1", "--slack", "1"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False)
                self.assertEqual(run.returncode, 1, run.stdout)  # never done
                argv = json.loads(sorted(argv_dir.iterdir())[-1].read_text())
                return argv

            def ledger_entry() -> dict:
                return json.loads((hd / "plans.json").read_text())[0]

            # Round 1: pi backend mints a session id, cold RESOLVER prompt,
            # no claude-only state/budget plumbing.
            argv = run_fanout()
            sid = argv[argv.index("--pi-session-id") + 1]
            self.assertEqual(argv[argv.index("--pi-session-dir") + 1],
                             str(hd / "pi-sessions"))
            self.assertNotIn("--state-file", argv)
            self.assertIn("Resolve the whole approved work unit", argv[-1])
            self.assertNotIn("context budget", argv[-1])
            entry = ledger_entry()
            self.assertEqual(entry["status"], "paused")
            self.assertEqual(entry["session"], sid)  # recorded for warm resume

            # Round 2: the pause resumes the SAME session with the resume prompt.
            argv = run_fanout()
            self.assertEqual(argv[argv.index("--pi-session-id") + 1], sid)
            self.assertIn("RESUMING", argv[-1])
            result = json.loads((hd / "fanout-result.json").read_text())[0]
            self.assertTrue(result["resumed"])
            self.assertEqual(ledger_entry()["session"], sid)

            # Round 3: a failure clears the session (set_status invalidates it).
            mode.write_text("fail")
            argv = run_fanout()
            self.assertEqual(ledger_entry()["status"], "failed")
            self.assertIsNone(ledger_entry()["session"])

            # Round 4: the retry cold-starts a fresh id at escalated effort.
            mode.write_text("pause")
            argv = run_fanout()
            fresh = argv[argv.index("--pi-session-id") + 1]
            self.assertNotEqual(fresh, sid)
            self.assertIn("Resolve the whole approved work unit", argv[-1])
            self.assertEqual(argv[argv.index("-t") + 1], "high")  # failed -> +1
            self.assertEqual(ledger_entry()["session"], fresh)  # new pause re-records

    def test_pi_provider_limit_failure_pauses_for_warm_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text(json.dumps([
                {"file": "plan.md", "title": "one", "wave": 0,
                 "model": "prov/model-x"}]))
            status.init(hd)
            argv_dir = root / "argv"
            argv_dir.mkdir()
            harness = root / "fake_harness.py"
            # Exits 1 after writing a session whose LAST assistant message is a
            # provider usage-limit error — the failure mode of a Codex limit.
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,sys,time\n"
                f"d=pathlib.Path({str(argv_dir)!r})\n"
                "d.joinpath(f'{time.monotonic_ns()}.json').write_text(json.dumps(sys.argv))\n"
                "sid=sys.argv[sys.argv.index('--pi-session-id')+1]\n"
                "sd=pathlib.Path(sys.argv[sys.argv.index('--pi-session-dir')+1])\n"
                "sd.mkdir(exist_ok=True)\n"
                "line=json.dumps({'type':'message','message':{'role':'assistant',"
                "'stopReason':'error',"
                "'errorMessage':'Codex error: The usage limit has been reached'}})\n"
                "(sd/f'2026-01-01T00-00-00-000Z_{sid}.jsonl').write_text(line+'\\n')\n"
                "sys.exit(1)\n"
            )
            harness.chmod(0o755)

            def run_fanout() -> list[str]:
                run = subprocess.run(
                    [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                     "--harness", str(harness), "--stopwatch", "5",
                     "--grace", "1", "--slack", "1"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False)
                self.assertEqual(run.returncode, 1, run.stdout)  # never done
                return json.loads(sorted(argv_dir.iterdir())[-1].read_text())

            def ledger_entry() -> dict:
                return json.loads((hd / "plans.json").read_text())[0]

            # Round 1: exit 1 + terminal limit error -> paused (not failed),
            # session recorded for warm resume, reason recorded in the ledger.
            argv = run_fanout()
            sid = argv[argv.index("--pi-session-id") + 1]
            entry = ledger_entry()
            self.assertEqual(entry["status"], "paused")
            self.assertEqual(entry["session"], sid)
            self.assertEqual(entry["pause_reason"], "provider-limit")
            result = json.loads((hd / "fanout-result.json").read_text())[0]
            self.assertEqual(result["status"], "paused")
            log = (hd / "logs" / "plan.log").read_text()
            self.assertIn("provider usage/rate limit", log)

            # Round 2: warm resume of the SAME session, effort NOT escalated
            # (a limit pause must never behave like a failure).
            argv = run_fanout()
            self.assertEqual(argv[argv.index("--pi-session-id") + 1], sid)
            self.assertIn("RESUMING", argv[-1])
            self.assertEqual(argv[argv.index("-t") + 1], "medium")

    def test_pi_retries_cold_with_full_prompt_despite_stale_claude_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: paused\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text(json.dumps([{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "model": "openai-codex/gpt-5.5", "session": "stale-claude-session",
                "pause_reason": "child-exit",
            }]))
            status.init(hd)
            argv_dump = root / "argv.jsonl"
            harness = root / "fake_harness.py"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,re,subprocess,sys\n"
                f"p=pathlib.Path({str(argv_dump)!r})\n"
                "rows=[] if not p.exists() else [json.loads(x) for x in p.read_text().splitlines()]\n"
                "rows.append(sys.argv); p.write_text(''.join(json.dumps(x)+'\\n' for x in rows))\n"
                "if len(rows) == 1: sys.exit(124)\n"
                "prompt=sys.argv[-1]\n"
                "m=re.search(r'\\\"([^\\\"]+)\\\" \\\"([^\\\"]+)\\\" --set \\\"([^\\\"]+)\\\" done --handover-dir \\\"([^\\\"]+)\\\"', prompt)\n"
                "subprocess.run([m.group(1),m.group(2),'--set',m.group(3),'done','--handover-dir',m.group(4)],check=True)\n"
            )
            harness.chmod(0o755)

            def run_fanout() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                     "--harness", str(harness), "--stopwatch", "5", "--grace", "1",
                     "--slack", "1"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False,
                )

            first = run_fanout()
            self.assertEqual(first.returncode, 1, first.stdout)
            self.assertIsNone(status._find(status._load_raw(hd), "plan.md")["session"])
            second = run_fanout()
            self.assertEqual(second.returncode, 0, second.stdout)

            rows = [json.loads(line) for line in argv_dump.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            for argv in rows:
                self.assertEqual(argv[argv.index("-m") + 1], "openai-codex/gpt-5.5")
                self.assertEqual(argv[argv.index("-s") + 1], "5")
                self.assertEqual(argv[argv.index("--max-context-tokens") + 1], "0")
                self.assertNotIn("--state-file", argv)
                self.assertNotIn("--resume-session", argv)
                self.assertIn("Resolve the whole approved work unit", argv[-1])
                self.assertNotIn("context budget", argv[-1])
            results = json.loads((hd / "fanout-result.json").read_text())
            self.assertFalse(results[0]["resumed"])

    def test_fanout_records_session_only_when_token_headroom_remains(self) -> None:
        for gauge, expected in ((100, "sess-rec-1"), (460000, None)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                hd = root / "handover"
                hd.mkdir()
                (hd / "goal.md").write_text("## Goal\nDo it\n")
                (hd / "plan.md").write_text(
                    "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
                )
                (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
                status.init(hd)
                harness = root / "fake_harness.py"
                harness.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json,sys\n"
                    "sf = sys.argv[sys.argv.index('--state-file')+1]\n"
                    "open(sf,'w').write(json.dumps({'session':'sess-rec-1',"
                    f"'reason':'child-exit','gauge':{gauge}}}))\n"
                )
                harness.chmod(0o755)
                run = subprocess.run(
                    [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                     "--harness", str(harness), "--stopwatch", "5", "--grace", "1",
                     "--slack", "1"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                )
                self.assertEqual(run.returncode, 1, run.stdout)  # exit 0 w/o done -> paused
                entry = status._find(status._load_raw(hd), "plan.md")
                self.assertEqual(entry["status"], "paused")
                self.assertEqual(entry["session"], expected, f"gauge={gauge}")
                self.assertEqual(entry["pause_reason"], "child-exit")

    def test_zero_progress_budget_loop_flags_needs_user_and_stops_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
            )
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            count = root / "count"
            harness = root / "fake_harness.py"
            # Simulates the guard killing the very first call on the token budget:
            # writes the sidecar (first_call_over_budget, full gauge) and exits 124.
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,sys\n"
                f"p=pathlib.Path({str(count)!r}); "
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
                "sf=sys.argv[sys.argv.index('--state-file')+1]\n"
                "open(sf,'w').write(json.dumps({'session':'sess-zp','reason':'token',"
                "'gauge':999999,'usage_events':1,'first_call_over_budget':True}))\n"
                "sys.exit(124)\n"
            )
            harness.chmod(0o755)

            def fanout() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                     "--harness", str(harness), "--stopwatch", "5", "--grace", "1",
                     "--slack", "1"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False,
                )

            # 1st zero-progress stop: paused, counted, NOT yet flagged, still open.
            self.assertEqual(fanout().returncode, 1)
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual((entry["status"], entry["zero_progress"]), ("paused", 1))
            self.assertIsNone(entry["session"])  # gauge is token-full: never warm
            self.assertNotIn("NEEDS-USER", (hd / "plan.md").read_text())

            # 2nd consecutive stop: flagged with a NEEDS-USER asking to split the plan.
            self.assertEqual(fanout().returncode, 1)
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual((entry["status"], entry["zero_progress"]), ("paused", 2))
            self.assertIn("NEEDS-USER: plan input alone", (hd / "plan.md").read_text())
            self.assertTrue(json.loads((hd / "fanout-result.json").read_text())[0]["needs_user"])

            # 3rd run must refuse to dispatch while the NEEDS-USER stands.
            self.assertEqual(fanout().returncode, 1)
            self.assertEqual(count.read_text(), "2")

    def test_live_lower_wave_gates_dispatchable_higher_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            for name in ("lower.md", "higher.md"):
                (hd / name).write_text(
                    "---\nstatus: pending\n---\n## Objective\nDo it\n\n## Progress / Handover\n"
                )
            (hd / "plans.json").write_text(json.dumps([
                {"file": "lower.md", "title": "lower", "wave": 0},
                {"file": "higher.md", "title": "higher", "wave": 1},
            ]))
            status.init(hd)
            self.assertEqual(status.claim(hd, "lower.md", 900), "claimed")
            marker = root / "called"
            harness = root / "must_not_run"
            harness.write_text(f"#!/bin/sh\ntouch {marker}\n")
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertFalse(marker.exists())
            results = json.loads((hd / "fanout-result.json").read_text())
            self.assertIn("claimed_elsewhere", {row["status"] for row in results})
            self.assertIn("skipped_dependency", {row["status"] for row in results})

    def test_prepaused_needs_user_is_compacted_before_pause_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text(
                "---\nstatus: paused\n---\n## Objective\nDo it\n\n"
                "## Progress / Handover\nNEEDS-USER: obsolete blocker\n"
                + ("historical output\n" * 100)
                + "NEEDS-USER: choose safe option A or B\n"
            )
            (hd / "plans.json").write_text(
                '[{"file":"plan.md","title":"one","wave":0,"status":"paused"}]'
            )
            status.init(hd)
            marker = root / "called"
            harness = root / "must_not_run"
            harness.write_text(f"#!/bin/sh\ntouch {marker}\n")
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--stopwatch", "5",
                 "--checkpoint-chars", "100"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertFalse(marker.exists())
            body = fanout._PROGRESS_RE.search((hd / "plan.md").read_text()).group(1).strip()
            self.assertLessEqual(len(body), 100)
            self.assertEqual(body.count("NEEDS-USER: "), 1)
            self.assertIn("NEEDS-USER: choose safe option A or B", body)
            self.assertTrue(json.loads((hd / "fanout-result.json").read_text())[0]["needs_user"])


class GuardTests(unittest.TestCase):
    BUDGET = guard.Budget(stopwatch=7200, grace=300,
                          max_context_tokens=470000, context_grace=40000)

    @staticmethod
    def _assistant(ctx: int, parent: str | None = None) -> dict:
        event = {"type": "assistant",
                 "message": {"usage": {"input_tokens": ctx,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0,
                                       "output_tokens": 0},
                             "content": []}}
        if parent is not None:
            event["parent_tool_use_id"] = parent
        return event

    def test_gauge_counts_only_main_chain_usage(self) -> None:
        self.assertEqual(guard.context_tokens(self._assistant(1000)), 1000)
        self.assertIsNone(guard.context_tokens(self._assistant(1000, parent="tu_1")))

    def test_gauge_uses_latest_value_not_running_max(self) -> None:
        # The run loop assigns (never maxes) — compaction can shrink the window.
        gauge = 0
        for event in (self._assistant(300000), self._assistant(120000)):
            tokens = guard.context_tokens(event)
            if tokens is not None:
                gauge = tokens
        self.assertEqual(gauge, 120000)

    def test_context_tokens_sums_usage_and_tolerates_gaps(self) -> None:
        event = {"type": "assistant", "message": {"usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30, "output_tokens": 40}}}
        self.assertEqual(guard.context_tokens(event), 100)
        self.assertEqual(guard.context_tokens(
            {"type": "assistant", "message": {"usage": {"input_tokens": 5}}}), 5)
        self.assertIsNone(guard.context_tokens({"type": "user"}))
        self.assertIsNone(guard.context_tokens({"type": "assistant", "message": {}}))

    def test_wind_down_trips_once_for_token_or_time(self) -> None:
        b = self.BUDGET
        self.assertEqual(guard.should_wind_down(430000, 0, b, False, False), "token")
        self.assertIsNone(guard.should_wind_down(429999, 0, b, False, False))
        self.assertEqual(guard.should_wind_down(0, 6900, b, False, False), "time")
        self.assertIsNone(guard.should_wind_down(0, 6899, b, False, False))
        self.assertIsNone(guard.should_wind_down(430000, 6900, b, True, False))  # latched
        self.assertIsNone(guard.should_wind_down(430000, 6900, b, False, True))  # after result
        off = guard.Budget(7200, 300, 0, 0)
        self.assertIsNone(guard.should_wind_down(10**9, 0, off, False, False))
        self.assertEqual(guard.should_wind_down(10**9, 6900, off, False, False), "time")
        # grace >= stopwatch must not fire the notice at t=0: capped at stopwatch/2
        short = guard.Budget(300, 300, 470000, 40000)
        self.assertIsNone(guard.should_wind_down(0, 149, short, False, False))
        self.assertEqual(guard.should_wind_down(0, 150, short, False, False), "time")

    def test_hard_stop_token_and_time_thresholds(self) -> None:
        b = self.BUDGET
        self.assertEqual(guard.should_hard_stop(470000, 0, b), "token")
        self.assertIsNone(guard.should_hard_stop(469999, 0, b))
        self.assertEqual(guard.should_hard_stop(0, 7200, b), "time")
        self.assertIsNone(guard.should_hard_stop(0, 7199.9, b))
        self.assertIsNone(guard.should_hard_stop(10**9, 0, guard.Budget(7200, 300, 0, 0)))

    def test_exit_code_mapping(self) -> None:
        self.assertEqual(guard.final_exit_code(True, 0), 124)
        self.assertEqual(guard.final_exit_code(False, 7), 7)
        self.assertEqual(guard.final_exit_code(False, -9), 137)
        self.assertEqual(guard.final_exit_code(False, -15), 143)

    def test_wind_down_message_is_valid_stream_json_user_line(self) -> None:
        line = guard.user_message_line(guard.wind_down_text("token", 430000, 470000, 0))
        event = json.loads(line.decode("utf-8"))
        self.assertEqual(event["type"], "user")
        text = event["message"]["content"][0]["text"]
        self.assertIn("Progress / Handover", text)
        self.assertIn("done", text)
        self.assertIn("42", guard.wind_down_text("time", 0, 470000, 42))

    def test_compact_log_truncates_tool_input_and_keeps_text(self) -> None:
        event = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working on it"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}},
        ]}}
        lines = guard.compact_log_lines(event, tool_input_chars=100)
        self.assertEqual(lines[0], "working on it")
        self.assertTrue(lines[1].startswith(">> Bash "))
        self.assertLessEqual(len(lines[1]), len(">> Bash ") + 101)
        event["parent_tool_use_id"] = "tu_1"
        self.assertEqual(guard.compact_log_lines(event), [])

    def test_guard_injects_wind_down_and_passes_through_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture"
            state = root / "state.json"
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "def emit(o): sys.stdout.write(json.dumps(o)+'\\n'); sys.stdout.flush()\n"
                "def ev(n): return {'type':'assistant','session_id':'sess-int-1',"
                "'message':{'usage':"
                "{'input_tokens':n},'content':[{'type':'text','text':'step %d'%n}]}}\n"
                "sys.stdin.readline()\n"
                "emit(ev(100))\n"
                "emit(ev(700))\n"
                f"open({str(capture)!r},'w').write(sys.stdin.readline())\n"
                "emit({'type':'result','num_turns':1,'result':'WRAPPED-UP',"
                "'session_id':'sess-int-1'})\n"
                "sys.stdin.read()\n"
            )
            fake.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_guard.py"), "--model", "claude-test",
                 "--claude-bin", str(fake), "--stopwatch", "30", "--grace", "5",
                 "--max-context-tokens", "1000", "--context-grace", "400",
                 "--state-file", str(state),
                 "--", "do the thing"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn("wind-down injected (token)", run.stdout)
            self.assertIn("WRAPPED-UP", run.stdout)
            injected = json.loads(capture.read_text())
            self.assertEqual(injected["type"], "user")
            self.assertIn("CASPER GUARD", injected["message"]["content"][0]["text"])
            sidecar = json.loads(state.read_text())
            self.assertEqual(sidecar["session"], "sess-int-1")
            self.assertEqual(sidecar["reason"], "child-exit")
            self.assertEqual(sidecar["gauge"], 700)

    def test_guard_hard_kills_child_that_ignores_wind_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys,time\n"
                "sys.stdin.readline()\n"
                "sys.stdout.write(json.dumps({'type':'assistant','message':"
                "{'usage':{'input_tokens':5000},'content':[]}})+'\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n"
            )
            fake.chmod(0o755)
            state = root / "state.json"
            run = subprocess.run(
                [PYTHON, str(WF / "casper_guard.py"), "--model", "claude-test",
                 "--claude-bin", str(fake), "--stopwatch", "30", "--grace", "5",
                 "--max-context-tokens", "1000", "--context-grace", "400",
                 "--state-file", str(state),
                 "--", "do the thing"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=30,
            )
            self.assertEqual(run.returncode, 124, run.stdout)
            self.assertIn("HARD token limit", run.stdout)
            sidecar = json.loads(state.read_text())
            self.assertEqual(sidecar["reason"], "token")
            self.assertTrue(sidecar["first_call_over_budget"])  # killed on call #1

    def test_guard_resume_flag_reaches_child_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv_dump = root / "argv.json"
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                f"open({str(argv_dump)!r},'w').write(json.dumps(sys.argv))\n"
                "sys.stdout.write(json.dumps({'type':'result','num_turns':0,"
                "'result':'ok','session_id':'sess-r2'})+'\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdin.read()\n"
            )
            fake.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_guard.py"), "--model", "claude-test",
                 "--claude-bin", str(fake), "--stopwatch", "30",
                 "--resume-session", "sess-r1", "--", "continue"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            argv = json.loads(argv_dump.read_text())
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-r1")

    def test_guard_falls_back_to_cold_start_when_resume_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "if '--resume' in sys.argv:\n"
                "    sys.stderr.write('No conversation found with session ID: x\\n')\n"
                "    sys.exit(1)\n"
                "sys.stdout.write(json.dumps({'type':'result','num_turns':0,"
                "'result':'cold-ok','session_id':'sess-new'})+'\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdin.read()\n"
            )
            fake.chmod(0o755)
            state = root / "state.json"
            run = subprocess.run(
                [PYTHON, str(WF / "casper_guard.py"), "--model", "claude-test",
                 "--claude-bin", str(fake), "--stopwatch", "30",
                 "--resume-session", "sess-dead", "--state-file", str(state),
                 "--", "continue"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn("falling back to a cold start", run.stdout)
            self.assertIn("cold-ok", run.stdout)
            sidecar = json.loads(state.read_text())
            self.assertEqual(sidecar["session"], "sess-new")
            self.assertFalse(sidecar["resumed"])  # final state is the cold attempt


class HarnessTests(unittest.TestCase):
    def test_current_claude_aliases_and_default_stay_on_claude(self) -> None:
        aliases = {
            None: "claude-opus-5",
            "fable": "claude-fable-5",
            "Fable 5": "claude-fable-5",
            "opus": "claude-opus-5",
            "Opus 5": "claude-opus-5",
            "sonnet": "claude-sonnet-5",
            "Sonnet 5": "claude-sonnet-5",
            "haiku": "claude-haiku-4-5-20251001",
            "Haiku 4.5": "claude-haiku-4-5-20251001",
            "claude-opus-4-7": "claude-opus-4-7",
        }
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                cmd = [str(WF / "LLM_harness.sh"), "--dry-run"]
                if alias is not None:
                    cmd += ["--model", alias]
                cmd += ["--", "quoted prompt"]
                run = subprocess.run(
                    cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertIn("claude -p", run.stdout)
                self.assertIn(f"--model {expected}", run.stdout)

    def test_model_aware_defaults_and_explicit_overrides_reach_each_backend(self) -> None:
        defaults = (
            # model, backend effort flag, expected effort
            (None, "--effort", "medium"),
            ("Opus 5", "--effort", "medium"),
            ("anthropic/claude-opus-5", "--thinking", "medium"),
            ("gpt-5.6-sol", "--thinking", "high"),
            ("openai-codex/gpt-5.6-sol", "--thinking", "high"),
            ("gpt-5.5", "--thinking", "medium"),
            ("sonnet", "--effort", "medium"),
        )
        for model, flag, expected in defaults:
            with self.subTest(model=model):
                cmd = [str(WF / "LLM_harness.sh"), "--dry-run"]
                if model is not None:
                    cmd += ["--model", model]
                cmd += ["--", "hi"]
                run = subprocess.run(
                    cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, "PI_OFFLINE": "1"}, check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertIn(f"{flag} {expected}", run.stdout)

        for model, flag in (("opus", "--effort"), ("gpt-5.6-sol", "--thinking")):
            with self.subTest(explicit_model=model):
                run = subprocess.run(
                    [str(WF / "LLM_harness.sh"), "--dry-run", "--model", model,
                     "--thinking", "low", "--", "hi"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, "PI_OFFLINE": "1"}, check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertIn(f"{flag} low", run.stdout)

    def test_harness_and_fanout_model_routing_stay_in_parity(self) -> None:
        # The token cap is claude-only: it is passed on the claude cases and an
        # explicit cap on a pi case is asserted to fail loudly further down.
        cases = (
            # model, Claude backend, resolved model, Pi provider args
            ("opus", True, "claude-opus-5", None),
            ("Fable 5", True, "claude-fable-5", None),
            ("claude-opus-4-7", True, "claude-opus-4-7", None),
            ("Claude-opus-4-7", False, "Claude-opus-4-7", "openai"),
            ("CLAUDE-opus-4-7", False, "CLAUDE-opus-4-7", "openai"),
            ("gpt-5.6-sol", False, "gpt-5.6-sol", "openai"),
            ("openai-codex/gpt-5.6-sol", False, "openai-codex/gpt-5.6-sol", None),
            ("claude/claude-opus-5", False, "claude/claude-opus-5", None),
            ("anthropic/claude-sonnet-5", False, "anthropic/claude-sonnet-5", None),
        )
        for model, is_claude, resolved, pi_provider in cases:
            with self.subTest(model=model):
                self.assertEqual(fanout._is_claude_model(model), is_claude)
                run = subprocess.run(
                    [str(WF / "LLM_harness.sh"), "--dry-run", "--model", model,
                     "--thinking", "max",
                     "--max-context-tokens", "470000" if is_claude else "0",
                     "--", "hi"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, "PI_OFFLINE": "1"}, check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout)
                if not is_claude:
                    # an explicit cap the pi route cannot honor is a usage error,
                    # never a silently dropped budget
                    capped = subprocess.run(
                        [str(WF / "LLM_harness.sh"), "--dry-run", "--model", model,
                         "--max-context-tokens", "470000", "--", "hi"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, "PI_OFFLINE": "1"}, check=False,
                    )
                    self.assertEqual(capped.returncode, 2, capped.stdout)
                    self.assertIn("needs the claude backend", capped.stdout)
                if is_claude:
                    self.assertIn("casper_guard.py", run.stdout)
                    self.assertIn(f"--model {resolved}", run.stdout)
                else:
                    self.assertIn("pi --mode rpc", run.stdout)  # rpc supervisor route
                    self.assertNotIn("casper_guard.py", run.stdout)
                    self.assertIn("casper_pi_guard.py", run.stdout)
                    self.assertIn("--stall-secs 900", run.stdout)
                    self.assertIn(f"--model {resolved}", run.stdout)
                    self.assertIn("--thinking max", run.stdout)
                    if pi_provider:
                        self.assertIn(f"--provider {pi_provider}", run.stdout)
                    else:
                        self.assertNotIn("--provider", run.stdout)

    def test_gpt_5_6_sol_forms_are_explicitly_listed(self) -> None:
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "--list-models"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PI_OFFLINE": "1"}, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout)
        bare = next(line for line in run.stdout.splitlines()
                    if line.startswith("gpt-5.6-sol"))
        qualified = next(line for line in run.stdout.splitlines()
                         if "openai-codex/gpt-5.6-sol" in line)
        self.assertIn("--provider openai", bare)
        self.assertIn("OPENAI_API_KEY", bare)
        self.assertNotIn("--provider", qualified)
        self.assertIn("ChatGPT OAuth/subscription", qualified)

    def test_fake_binaries_preserve_prompt_stdout_and_child_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "argv.json"
            fake = (
                "#!/usr/bin/env python3\n"
                "import json,os,pathlib,sys,time\n"
                "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv))\n"
                "sys.stdout.write(os.environ.get('FAKE_STDOUT','')); sys.stdout.flush()\n"
                "time.sleep(float(os.environ.get('FAKE_SLEEP','0')))\n"
                "sys.exit(int(os.environ.get('FAKE_EXIT','0')))\n"
            )
            for name in ("claude", "pi"):
                path = root / name
                path.write_text(fake)
                path.chmod(0o755)
            prompt = "quote ' and spaces $HOME"
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "CAPTURE": str(capture), "FAKE_STDOUT": "raw-model-output\n",
                   "FAKE_EXIT": "23"}
            run = subprocess.run(
                [str(WF / "LLM_harness.sh"), "--model", "gpt-5.5",
                 "--thinking", "max", "--stopwatch", "5",
                 "--pi-stall-secs", "0",  # bare-route contract: byte-for-byte stdout
                 "--", prompt],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=False,
            )
            self.assertEqual(run.returncode, 23)
            self.assertEqual(run.stdout, "raw-model-output\n")
            self.assertEqual(run.stderr, "")
            argv = json.loads(capture.read_text())
            self.assertEqual(Path(argv[0]).name, "pi")
            # pi has no `--` end-of-options separator; the prompt is positional.
            self.assertEqual(argv[1:], ["-p", "--provider", "openai", "--model",
                                       "gpt-5.5", "--thinking", "max", prompt])

            env.update({"FAKE_STDOUT": "claude-output\n", "FAKE_EXIT": "19"})
            claude_run = subprocess.run(
                [str(WF / "LLM_harness.sh"), "--model", "opus",
                 "--thinking", "high", "--stopwatch", "5", "--", prompt],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=False,
            )
            self.assertEqual(claude_run.returncode, 19)
            self.assertEqual(claude_run.stdout, "claude-output\n")
            self.assertEqual(claude_run.stderr, "")
            argv = json.loads(capture.read_text())
            self.assertEqual(Path(argv[0]).name, "claude")
            self.assertEqual(argv[1:], ["-p", "--dangerously-skip-permissions", "--model",
                                       "claude-opus-5", "--effort", "high", "--", prompt])

    def test_wall_clock_timeout_returns_124(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nsleep 30\n")
            fake_pi.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}
            run = subprocess.run(
                [str(WF / "LLM_harness.sh"), "--model", "gpt-5.5",
                 "--stopwatch", "1", "--", "hi"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, check=False, timeout=5,
            )
            self.assertEqual(run.returncode, 124, run.stdout)

    def test_dry_run_routes_claude_through_guard_only_when_enabled(self) -> None:
        base = [str(WF / "LLM_harness.sh"), "-n", "-m", "opus", "-s", "7200"]
        with_guard = subprocess.run(
            base + ["--max-context-tokens", "470000", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(with_guard.returncode, 0, with_guard.stdout)
        self.assertIn("casper_guard.py", with_guard.stdout)
        self.assertIn("7260", with_guard.stdout)  # failsafe = stopwatch + 60
        without = subprocess.run(
            base + ["--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(without.returncode, 0, without.stdout)
        self.assertNotIn("casper_guard.py", without.stdout)  # verify path unchanged
        self.assertIn("claude", without.stdout)

    def test_dry_run_pi_stall_secs_zero_disables_pi_guard(self) -> None:
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "-n", "-m", "gpt-5.6-sol",
             "--pi-stall-secs", "0", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertNotIn("casper_pi_guard.py", run.stdout)
        self.assertIn("pi -p", run.stdout)

    def test_default_model_flag_is_driver_aware(self) -> None:
        cases = (
            ({}, "opus"),
            (PI_DRIVER_ENV, "anthropic/claude-opus-5"),
            # PI_MODEL alone must NOT be used: the bare claude-* id would route
            # to the Claude CLI, silently jumping drivers.
            ({"PI_CODING_AGENT": "true", "PI_MODEL": "claude-opus-5"}, "opus"),
            ({"PI_CODING_AGENT": "true", "PI_PROVIDER": "anthropic"}, "opus"),
        )
        for extra, expected in cases:
            with self.subTest(pi_env=sorted(extra), expected=expected):
                run = subprocess.run(
                    [str(WF / "LLM_harness.sh"), "--default-model"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, **extra}, check=False)
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertEqual(run.stdout.strip(), expected)

    def test_no_model_dispatch_follows_the_driver(self) -> None:
        pi_env = {**os.environ, **PI_DRIVER_ENV}
        # pi driver, no --model -> the driver's own model on the pi route
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "--dry-run", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=pi_env, check=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("pi --mode rpc", run.stdout)
        self.assertIn("--model anthropic/claude-opus-5", run.stdout)
        self.assertIn("--thinking medium", run.stdout)
        # an explicit --model always beats the driver default — but a bare
        # claude-* id still follows the driver's credentials (pi route)
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "--dry-run", "--model", "sonnet", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=pi_env, check=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("pi --mode rpc", run.stdout)
        self.assertNotIn("claude -p", run.stdout)
        self.assertIn("--model anthropic/claude-sonnet-5", run.stdout)
        # --model "" is the documented auto knob (casper.md MODEL="")
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "--dry-run", "--model", "", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=pi_env, check=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("--model anthropic/claude-opus-5", run.stdout)

    def test_bare_claude_follows_the_pi_driver_credentials(self) -> None:
        # Under a pi driver a bare claude-* id (alias or full id) must run on pi
        # as anthropic/<id>: pi reads $PI_CODING_AGENT_DIR/auth.json, i.e. the
        # pi-<profile> that launched the driver, whereas the Claude CLI reads
        # $CLAUDE_CONFIG_DIR (default ~/.claude), which no pi profile sets.
        # PI_CODING_AGENT alone is the driver signal; PI_PROVIDER/PI_MODEL only
        # matter for composing the no-model default. Non-Claude ids are untouched.
        cases = (
            # model, Claude backend, harness --model, Pi provider args
            ("opus", False, "anthropic/claude-opus-5", None),
            ("Fable 5", False, "anthropic/claude-fable-5", None),
            ("claude-opus-5", False, "anthropic/claude-opus-5", None),
            ("anthropic/claude-sonnet-5", False, "anthropic/claude-sonnet-5", None),
            ("gpt-5.6-sol", False, "gpt-5.6-sol", "openai"),
            ("openai-codex/gpt-5.6-sol", False, "openai-codex/gpt-5.6-sol", None),
        )
        for extra in (PI_DRIVER_ENV, {"PI_CODING_AGENT": "true"}):
            for model, is_claude, resolved, pi_provider in cases:
                with self.subTest(pi_env=sorted(extra), model=model):
                    with unittest.mock.patch.dict(os.environ, extra):
                        self.assertEqual(fanout._is_claude_model(model), is_claude)
                    run = subprocess.run(
                        [str(WF / "LLM_harness.sh"), "--dry-run", "--model", model,
                         "--max-context-tokens", "0", "--", "hi"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, **extra, "PI_OFFLINE": "1"}, check=False,
                    )
                    self.assertEqual(run.returncode, 0, run.stdout)
                    self.assertIn("pi --mode rpc", run.stdout)
                    self.assertNotIn("claude -p", run.stdout)
                    self.assertNotIn("casper_guard.py", run.stdout)
                    self.assertIn(f"--model {resolved}", run.stdout)
                    if pi_provider:
                        self.assertIn(f"--provider {pi_provider}", run.stdout)
                    else:
                        self.assertNotIn("--provider", run.stdout)
                    # the driver route has no token guard, so an explicit cap on
                    # it is refused instead of silently dropped
                    capped = subprocess.run(
                        [str(WF / "LLM_harness.sh"), "--dry-run", "--model", model,
                         "--max-context-tokens", "470000", "--", "hi"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, **extra, "PI_OFFLINE": "1"}, check=False,
                    )
                    self.assertEqual(capped.returncode, 2, capped.stdout)
                    self.assertIn("needs the claude backend", capped.stdout)
        # Outside pi the Claude CLI route is unchanged (env stripped at import).
        self.assertTrue(fanout._is_claude_model("claude-opus-5"))
        run = subprocess.run(
            [str(WF / "LLM_harness.sh"), "--dry-run", "--model", "claude-opus-5",
             "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PI_OFFLINE": "1"}, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("claude -p", run.stdout)
        self.assertIn("--model claude-opus-5", run.stdout)

    def test_fanout_default_model_stays_in_parity_with_harness(self) -> None:
        probe = ("import sys; sys.path.insert(0, '.'); "
                 "import casper_fanout as f; print(f._default_model())")
        for extra in ({}, PI_DRIVER_ENV):
            with self.subTest(pi_env=sorted(extra)):
                env = {**os.environ, **extra}
                harness = subprocess.run(
                    [str(WF / "LLM_harness.sh"), "--default-model"],
                    text=True, stdout=subprocess.PIPE, env=env, check=False)
                fanout_default = subprocess.run(
                    [PYTHON, "-c", probe], text=True, stdout=subprocess.PIPE,
                    env=env, cwd=WF, check=False)
                self.assertEqual(harness.stdout.strip(),
                                 fanout_default.stdout.strip())

    def test_pi_session_flags_create_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp) / "pi-sessions"
            sd.mkdir()
            base = [str(WF / "LLM_harness.sh"), "--dry-run",
                    "--model", "anthropic/claude-opus-5",
                    "--pi-session-id", "abc-123",
                    "--pi-session-dir", str(sd), "--", "hi"]
            run = subprocess.run(base, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn(f"--session-dir {sd}", run.stdout)
            self.assertIn("--session-id abc-123", run.stdout)
            self.assertIn(f"--watch-dir {sd}", run.stdout)  # pi-guard liveness
            # an existing session file for that id flips create -> file resume,
            # which works from any cwd (plain --session-id lookup is cwd-scoped)
            newest = sd / "2026-01-02T00-00-00-000Z_abc-123.jsonl"
            (sd / "2026-01-01T00-00-00-000Z_abc-123.jsonl").write_text("{}\n")
            newest.write_text("{}\n")
            run = subprocess.run(base, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn(f"--session {newest}", run.stdout)  # newest file wins
            self.assertNotIn("--session-id", run.stdout)
            # claude routes ignore the pi session flags entirely
            run = subprocess.run(
                [str(WF / "LLM_harness.sh"), "--dry-run", "--model", "opus",
                 "--pi-session-id", "abc-123", "--pi-session-dir", str(sd),
                 "--", "hi"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertNotIn("--session", run.stdout)


# Scripted stand-in for `pi --mode rpc`: JSONL commands in, JSONL events out.
FAKE_RPC = """#!/usr/bin/env python3
import json, sys, threading, time
scenario = sys.argv[1]

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

got_steer = threading.Event()
got_ui_cancel = threading.Event()

def read_stdin():
    for line in sys.stdin:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if c.get("type") == "steer":
            got_steer.set()
        if c.get("type") == "extension_ui_response" and c.get("cancelled"):
            got_ui_cancel.set()

threading.Thread(target=read_stdin, daemon=True).start()
time.sleep(0.2)
emit({"type": "agent_start"})

def finish(text, stop="stop"):
    emit({"type": "message_end", "message": {"role": "assistant",
          "content": [{"type": "text", "text": text}], "stopReason": stop}})
    emit({"type": "agent_end"})
    emit({"type": "agent_settled"})
    time.sleep(0.5)

if scenario == "happy":
    finish("FINAL ANSWER")
elif scenario == "error":
    finish("boom", stop="error")
elif scenario == "ui":
    emit({"type": "extension_ui_request", "id": "u1", "method": "confirm",
          "title": "Allow dangerous command?"})
    finish("UI CANCELLED" if got_ui_cancel.wait(10) else "NO RESPONSE")
elif scenario == "winddown":
    for _ in range(200):
        if got_steer.is_set():
            finish("WOUND DOWN")
            break
        emit({"type": "turn_start"}); time.sleep(0.3)
elif scenario == "ignore":
    while True:
        emit({"type": "turn_start"}); time.sleep(0.3)
elif scenario == "silent":
    time.sleep(120)
"""


class PiGuardTests(unittest.TestCase):
    """casper_pi_guard.py behavior with real child processes (no pi involved)."""

    GUARD = WF / "casper_pi_guard.py"

    def test_dedicated_watch_dir_ignores_same_named_foreign_sessions(self) -> None:
        """A watch dir is this run's only activity signal.

        The needle is basename(cwd), so the shared trees also match unrelated
        sessions in a worktree of the same name. Unioning them reset the stall
        timer on every foreign write and blinded the watchdog.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            needle = "my-worktree"
            # Foreign-but-same-basename sessions: the driving session and one of
            # its subagents. Both are NEWER than this resolver's own session.
            for sub in ("sessions", "subagent-sessions"):
                foreign = root / "agent" / sub / f"--home-user-worktrees-{needle}--"
                foreign.mkdir(parents=True)
                busy = foreign / "foreign.jsonl"
                busy.write_text("{}\n")
                os.utime(busy, (5000.0, 5000.0))

            watch = root / "handover" / "pi-sessions"
            watch.mkdir(parents=True)
            mine = watch / "resolver.jsonl"
            mine.write_text("{}\n")
            os.utime(mine, (1000.0, 1000.0))  # stalled long before the foreign writes

            session_root = root / "agent"
            self.assertEqual(
                pi_guard.newest_session_mtime(session_root, needle, (watch,)),
                1000.0,
                "foreign same-basename sessions must not count as activity",
            )
            # Without a dedicated dir, the shared trees remain the fallback.
            self.assertEqual(
                pi_guard.newest_session_mtime(session_root, needle, ()),
                5000.0,
            )

    def _run(self, guard_args: list[str], child: list[str],
             timeout: int = 60) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as empty_session_root:
            return subprocess.run(
                [PYTHON, str(self.GUARD), "--session-root", empty_session_root,
                 *guard_args, "--", *child],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=timeout,
            )

    def test_propagates_child_exit_stdout_and_quiet_stderr(self) -> None:
        run = self._run(
            ["--stall-secs", "60", "--stopwatch", "60", "--poll-secs", "1"],
            [PYTHON, "-c", "print('raw-model-output'); raise SystemExit(23)"],
        )
        self.assertEqual(run.returncode, 23)
        self.assertEqual(run.stdout, "raw-model-output\n")
        self.assertEqual(run.stderr, "")  # happy path is byte-silent

    def test_stall_kills_silent_child_with_pause_exit(self) -> None:
        run = self._run(
            ["--stall-secs", "2", "--grace", "1", "--poll-secs", "1",
             "--stopwatch", "120"],
            [PYTHON, "-c", "import time; time.sleep(120)"],
            timeout=40,
        )
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertIn("[pi-guard]", run.stderr)
        self.assertIn("no activity", run.stderr)

    def test_stopwatch_kills_active_child_with_pause_exit(self) -> None:
        run = self._run(
            ["--stall-secs", "600", "--poll-secs", "1", "--stopwatch", "2"],
            [PYTHON, "-c", "while True: pass"],
            timeout=40,
        )
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertIn("stopwatch", run.stderr)

    def test_unsticks_wedged_pi_leaf_and_run_recovers(self) -> None:
        # A fake `pi` root that spawns a fake `pi` leaf and blocks on it —
        # the shape of a hung subagent streaming call. The guard must TERM
        # only the leaf; the root then "recovers" and exits 0 on its own.
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = Path(tmp) / "pi"
            fake_pi.write_text(
                "#!" + PYTHON + "\n"
                "import subprocess, sys, time\n"
                "if '--leaf' in sys.argv:\n"
                "    time.sleep(300)\n"
                "    sys.exit(0)\n"
                "leaf = subprocess.Popen([sys.argv[0], '--leaf'])\n"
                "leaf.wait()\n"
                "print('RECOVERED')\n"
                "sys.exit(0)\n"
            )
            fake_pi.chmod(0o755)
            run = self._run(
                ["--stall-secs", "2", "--grace", "5", "--poll-secs", "1",
                 "--stopwatch", "120"],
                [str(fake_pi)],
                timeout=60,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("RECOVERED", run.stdout)
        self.assertIn("unstick 1/", run.stderr)

    def test_watch_dir_jsonl_writes_count_as_liveness(self) -> None:
        # A silent sleeper would be stall-killed (proved above); the same child
        # survives when something keeps writing *.jsonl in a --watch-dir.
        with tempfile.TemporaryDirectory() as tmp:
            watch = Path(tmp) / "pi-sessions"
            watch.mkdir()
            toucher = subprocess.Popen(
                [PYTHON, "-c",
                 "import sys,time\n"
                 "p=sys.argv[1]\n"
                 "for _ in range(30):\n"
                 "    open(p,'a').write('x\\n'); time.sleep(0.3)\n",
                 str(watch / "s.jsonl")])
            try:
                run = self._run(
                    ["--stall-secs", "2", "--grace", "1", "--poll-secs", "1",
                     "--stopwatch", "60", "--watch-dir", str(watch)],
                    [PYTHON, "-c", "import time; time.sleep(6)"],
                    timeout=40,
                )
            finally:
                toucher.terminate()
                toucher.wait()
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_session_root_defaults_to_pi_coding_agent_dir(self) -> None:
        # No explicit --session-root: the guard derives it from
        # $PI_CODING_AGENT_DIR and counts session writes under <dir>/sessions.
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "agent"
            (agent_dir / "sessions").mkdir(parents=True)
            sess = agent_dir / "sessions" / "s.jsonl"
            toucher = subprocess.Popen(
                [PYTHON, "-c",
                 "import sys,time\n"
                 "p=sys.argv[1]\n"
                 "for _ in range(30):\n"
                 "    open(p,'a').write('x\\n'); time.sleep(0.3)\n",
                 str(sess)])
            try:
                run = subprocess.run(
                    [PYTHON, str(self.GUARD), "--stall-secs", "2", "--grace", "1",
                     "--poll-secs", "1", "--stopwatch", "60",
                     "--", PYTHON, "-c", "import time; time.sleep(6)"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=40, cwd=tmp,
                    env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)},
                )
            finally:
                toucher.terminate()
                toucher.wait()
            self.assertEqual(run.returncode, 0, run.stderr)

    def _rpc_run(self, scenario: str, guard_args: list[str],
                 timeout: int = 40) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_rpc.py"
            fake.write_text(FAKE_RPC)
            return subprocess.run(
                [PYTHON, str(self.GUARD), "--session-root", tmp,
                 "--rpc-prompt", "resolve the plan", *guard_args,
                 "--", PYTHON, str(fake), scenario],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=timeout,
            )

    def test_rpc_happy_path_prints_final_text_and_exits_zero(self) -> None:
        run = self._rpc_run("happy", ["--stopwatch", "30", "--stall-secs", "20",
                                      "--poll-secs", "1"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "FINAL ANSWER\n")
        self.assertEqual(run.stderr, "")  # happy path is byte-silent

    def test_rpc_error_stop_reason_exits_one(self) -> None:
        run = self._rpc_run("error", ["--stopwatch", "30", "--stall-secs", "20",
                                      "--poll-secs", "1"])
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(run.stdout, "boom\n")

    def test_rpc_cancels_extension_dialogs(self) -> None:
        run = self._rpc_run("ui", ["--stopwatch", "30", "--stall-secs", "20",
                                   "--poll-secs", "1"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "UI CANCELLED\n")
        self.assertIn("cancelled extension dialog", run.stderr)

    def test_rpc_wind_down_is_steered_before_the_stopwatch(self) -> None:
        run = self._rpc_run("winddown", ["--stopwatch", "8", "--wind-down-secs", "5",
                                         "--stall-secs", "30", "--poll-secs", "0.5"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "WOUND DOWN\n")
        self.assertIn("wind-down notice steered", run.stderr)

    def test_rpc_oversized_wind_down_window_is_disabled(self) -> None:
        # wind-down-secs >= stopwatch: steering at t=0 would pollute the run
        run = self._rpc_run("happy", ["--stopwatch", "20", "--wind-down-secs", "300",
                                      "--stall-secs", "20", "--poll-secs", "1"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "FINAL ANSWER\n")
        self.assertNotIn("wind-down", run.stderr)

    def test_rpc_stopwatch_aborts_then_kills_with_pause_exit(self) -> None:
        run = self._rpc_run("ignore", ["--stopwatch", "2", "--stall-secs", "60",
                                       "--poll-secs", "1"])
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertIn("abort sent", run.stderr)
        self.assertIn("abort not honored", run.stderr)

    def test_rpc_stall_kills_silent_run_with_pause_exit(self) -> None:
        run = self._rpc_run("silent", ["--stopwatch", "60", "--stall-secs", "2",
                                       "--grace", "1", "--poll-secs", "1"])
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertIn("no activity", run.stderr)

    def test_rpc_child_that_never_speaks_is_killed_at_stopwatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run(
                [PYTHON, str(self.GUARD), "--session-root", tmp,
                 "--rpc-prompt", "x", "--stopwatch", "1", "--stall-secs", "60",
                 "--poll-secs", "1",
                 "--", PYTHON, "-c", "import time; time.sleep(30)"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=20,
            )
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertIn("stopwatch", run.stderr)


class VerifierTests(unittest.TestCase):
    GOAL = """## Goal
Do it.

## Acceptance Criteria
1. Commands work.
2. Result is clear.

## Verification
1. command (timeout=7)
   ```bash
   printf ok
   ```
2. judgment: Inspect the result for clarity.
"""

    def _done_handover(self, root: Path, goal: str) -> Path:
        hd = root / "handover"
        hd.mkdir()
        (hd / "goal.md").write_text(goal)
        (hd / "plan.md").write_text("---\nstatus: done\n---\n")
        (hd / "plans.json").write_text(
            json.dumps([{"file": "plan.md", "title": "one", "wave": 0, "status": "done"}])
        )
        status.init(hd)
        return hd

    def test_parse_command_and_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            goal = Path(tmp) / "goal.md"
            goal.write_text(self.GOAL)
            checks = verify.parse_goal(goal)
            self.assertEqual([(c.kind, c.timeout) for c in checks], [("command", 7), ("judgment", 300)])
            self.assertEqual(checks[0].body, "printf ok")

    def test_bracketed_verification_labels_parse_clean_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            goal = Path(tmp) / "goal.md"
            goal.write_text("""## Goal
Do it.
## Acceptance Criteria
1. Commands work.
2. Result is clear.
## Verification
1. [command] printf ok
2. [judgment] Inspect the result for clarity.
""")
            checks = verify.parse_goal(goal)
            self.assertEqual([c.kind for c in checks], ["command", "judgment"])
            self.assertEqual(checks[0].body, "printf ok")
            self.assertEqual(checks[1].body, "Inspect the result for clarity.")

    def test_command_only_path_never_invokes_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = self.GOAL.replace(
                "2. Result is clear.\n", ""
            ).replace("2. judgment: Inspect the result for clarity.\n", "")
            hd = self._done_handover(root, goal)
            marker = root / "called"
            harness = root / "must_not_run"
            harness.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                 "--cwd", str(root), "--harness", str(harness)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads((hd / "verify.json").read_text())[0]["status"], "pass")

    def test_all_subjective_criteria_use_one_batched_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = """## Goal
Review.
## Acceptance Criteria
1. Clear.
2. Consistent.
## Verification
1. judgment: Check clarity.
2. judgment: Check consistency.
"""
            hd = self._done_handover(root, goal)
            count = root / "count"
            harness = root / "judge"
            harness.write_text(
                "#!/bin/sh\n"
                f"n=0; [ ! -f {count} ] || n=$(cat {count}); echo $((n+1)) > {count}\n"
                "printf '%s\\n' '[{\"index\":0,\"status\":\"pass\",\"evidence\":\"clear\"},{\"index\":1,\"status\":\"pass\",\"evidence\":\"consistent\"}]'\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--judgment-stopwatch", "5"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertEqual(count.read_text().strip(), "1")
            self.assertEqual(len(json.loads((hd / "verify.json").read_text())), 2)

    def test_omitted_verification_method_fails_contract_without_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = """## Goal
Review.
## Acceptance Criteria
1. Command works.
2. Result is clear.
## Verification
1. command: printf ok
"""
            hd = self._done_handover(root, goal)
            marker = root / "called"
            harness = root / "must_not_run"
            harness.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                 "--cwd", str(root), "--harness", str(harness)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertFalse(marker.exists())
            results = json.loads((hd / "verify.json").read_text())
            self.assertEqual(results[0]["criterion"], "verification contract")
            self.assertIn("counts differ", results[0]["evidence"])

    def test_mixed_verification_delegates_only_the_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command_marker = root / "command-ran"
            goal = f"""## Goal
Review.
## Acceptance Criteria
1. Command works.
2. Result is clear.
## Verification
1. command: printf command-ok > {command_marker}
2. judgment: Inspect only the final result for clarity.
"""
            hd = self._done_handover(root, goal)
            argv_dump = root / "judge-argv.json"
            harness = root / "judge"
            harness.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,sys\n"
                f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))\n"
                "print('[{\"index\":1,\"status\":\"pass\","
                "\"evidence\":\"clear\"}]')\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                 "--cwd", str(root), "--harness", str(harness),
                 "--judgment-stopwatch", "5"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(command_marker.exists())
            prompt = json.loads(argv_dump.read_text())[-1]
            self.assertIn("Inspect only the final result for clarity.", prompt)
            self.assertNotIn("printf command-ok", prompt)
            self.assertNotIn('"index": 0', prompt)
            self.assertEqual(
                [row["status"] for row in json.loads((hd / "verify.json").read_text())],
                ["pass", "pass"],
            )

    def test_omitted_judgment_result_fails_the_delegated_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = """## Goal
Review.
## Acceptance Criteria
1. Clear.
2. Consistent.
## Verification
1. judgment: Check clarity.
2. judgment: Check consistency.
"""
            hd = self._done_handover(root, goal)
            harness = root / "incomplete-judge"
            harness.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '[{\"index\":0,\"status\":\"pass\","
                "\"evidence\":\"clear\"}]'\n"
            )
            harness.chmod(0o755)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                 "--harness", str(harness), "--judgment-stopwatch", "5"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            results = json.loads((hd / "verify.json").read_text())
            self.assertEqual([row["status"] for row in results], ["fail", "fail"])
            self.assertTrue(all("batched judgment failed" in row["evidence"]
                                for row in results))

    def test_command_timeout_fails_with_bounded_evidence(self) -> None:
        check = verify.Check("slow", "command", "printf '%0500d' 0; sleep 2", 0.05)
        with tempfile.TemporaryDirectory() as tmp:
            result = verify.run_command(check, Path(tmp), evidence_chars=120)
        self.assertEqual(result["status"], "fail")
        self.assertLessEqual(len(result["evidence"]), 120)
        self.assertIn("truncated", result["evidence"])


class CleanupTests(unittest.TestCase):
    def test_cleanup_refuses_live_in_progress_plan_even_with_passing_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hd = Path(tmp) / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text("## Goal\nDo it\n")
            (hd / "plan.md").write_text("---\nstatus: pending\n---\n")
            (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
            status.init(hd)
            status.claim(hd, "plan.md", 900)
            (hd / "verify.json").write_text('[{"criterion":"x","status":"pass","evidence":"ok"}]')
            run = subprocess.run(
                [PYTHON, str(WF / "casper_cleanup.py"), "--handover-dir", str(hd)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertTrue(hd.exists())


class CleanupSweepTests(unittest.TestCase):
    """--sweep ROOT: bulk reap of leftover run dirs (renamed ones included)."""

    def _run_dir(self, root: Path, name: str, *, plan_status: str = "done",
                 verify: str | None = '[{"criterion":"x","status":"pass","evidence":"ok"}]',
                 goal: bool = True, claim: bool = False) -> Path:
        hd = root / name
        hd.mkdir(parents=True)
        if goal:
            (hd / "goal.md").write_text("## Goal\nDo it\n")
        (hd / "plan.md").write_text("---\nstatus: pending\n---\n")
        (hd / "plans.json").write_text('[{"file":"plan.md","title":"one","wave":0}]')
        status.init(hd)
        if claim:
            self.assertEqual(status.claim(hd, "plan.md", 900), "claimed")
        else:
            status.set_status(hd, "plan.md", plan_status)
        if verify is not None:
            (hd / "verify.json").write_text(verify)
        return hd

    def _sweep(self, root: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(WF / "casper_cleanup.py"), "--sweep", str(root), *extra],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def test_sweep_removes_resolved_and_verified_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = self._run_dir(root, "goal-archived")
            run = self._sweep(root)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertFalse(hd.exists(), run.stdout)
            self.assertIn("removed", run.stdout)
            self.assertIn("1 removed", run.stdout)
            self.assertTrue(root.exists())  # ROOT itself is never touched

    def test_sweep_refuses_unverified_child_and_leaves_it_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = self._run_dir(root, "goal-unverified", verify=None)
            run = self._sweep(root)
            self.assertEqual(run.returncode, 0, run.stdout)  # refusal is informational
            self.assertTrue(hd.exists(), run.stdout)
            self.assertIn("refused", run.stdout)
            self.assertIn("1 refused", run.stdout)

    def test_sweep_never_deletes_live_in_progress_lease_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._run_dir(root, "goal-live", claim=True)
            run = self._sweep(root, "--force")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(live.exists(), run.stdout)
            self.assertIn("skipped-live-lease", run.stdout)
            self.assertIn("1 skipped-live-lease", run.stdout)
            self.assertEqual(status._find(status._load_raw(live), "plan.md")["status"],
                             "in_progress")

    def test_sweep_force_removes_unverified_but_not_the_live_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._run_dir(root, "goal-live", claim=True)
            junk = self._run_dir(root, "goal-superseded", plan_status="failed", verify=None)
            run = self._sweep(root, "--force")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(live.exists(), run.stdout)
            self.assertFalse(junk.exists(), run.stdout)

    def test_sweep_dry_run_deletes_nothing_and_reports_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = self._run_dir(root, "goal-done")
            run = self._sweep(root, "--dry-run")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(hd.exists(), run.stdout)
            self.assertIn("would-remove", run.stdout)
            self.assertIn("1 would-remove", run.stdout)
            self.assertIn("0 bytes reclaimed", run.stdout)

    def test_sweep_min_age_days_skips_a_freshly_touched_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = self._run_dir(root, "goal-fresh")
            (fresh / "notes.md").write_text("just now\n")
            run = self._sweep(root, "--min-age-days", "1")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(fresh.exists(), run.stdout)
            self.assertIn("skipped-too-new", run.stdout)
            self.assertIn("1 skipped-too-new", run.stdout)
            # aged past the threshold, the same child is reaped
            old = time.time() - 5 * 86400
            for path in [fresh, *fresh.rglob("*")]:
                os.utime(path, (old, old))
            run = self._sweep(root, "--min-age-days", "1")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertFalse(fresh.exists(), run.stdout)

    def test_sweep_refuses_child_without_goal_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stranger = self._run_dir(root, "not-a-run", goal=False)
            run = self._sweep(root, "--force")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue(stranger.exists(), run.stdout)
            self.assertIn("no goal.md", run.stdout)
            self.assertIn("1 refused", run.stdout)

    def test_sweep_ignores_files_and_does_not_recurse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "loose.txt").write_text("not a dir\n")
            nested = self._run_dir(root, "outer/inner")
            run = self._sweep(root, "--force")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertTrue((root / "loose.txt").exists())
            self.assertTrue(nested.exists(), run.stdout)  # depth 2 is out of scope
            self.assertIn("refused", run.stdout)  # outer/ has no goal.md

    def test_sweep_and_handover_dir_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run(
                [PYTHON, str(WF / "casper_cleanup.py"), "--sweep", tmp,
                 "--handover-dir", tmp],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 2, run.stdout)
            run = subprocess.run(
                [PYTHON, str(WF / "casper_cleanup.py")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(run.returncode, 2, run.stdout)


if __name__ == "__main__":
    unittest.main()
