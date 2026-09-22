"""Backend selection: --backend auto|pi|claude across harness, fanout and verifier.

Covers the axis that is independent of model/effort/profile: which CLI actually
runs a dispatch, which token cap it may carry, and which recorded session it is
allowed to warm-resume. Everything here runs against fake CLIs — no provider call,
no auth file is read or written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WF = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WF))

import casper_fanout as fanout
import casper_status as status

PYTHON = sys.executable
HARNESS = str(WF / "LLM_harness.sh")

for _var in ("PI_CODING_AGENT", "PI_PROVIDER", "PI_MODEL"):
    os.environ.pop(_var, None)

# A pi driver whose own model is a GPT: the derived default model is
# openai-codex/gpt-6-astra, so an explicitly Claude-backed resolver must be given
# a Claude model rather than silently inheriting the driver's.
GPT_DRIVER_ENV = {"PI_CODING_AGENT": "true", "PI_PROVIDER": "openai-codex",
                  "PI_MODEL": "gpt-6-astra", "PI_CODING_AGENT_DIR": "/nonexistent/pi-profile"}

# Stand-in for LLM_harness.sh: records argv per call, mimics the session-file and
# sidecar side effects the real backends have, then optionally marks the plan done.
FAKE_HARNESS = '''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
d = pathlib.Path(os.environ["ARGV_DIR"]); d.mkdir(parents=True, exist_ok=True)
(d / f"{time.monotonic_ns()}.json").write_text(json.dumps(sys.argv))
if "--pi-session-id" in sys.argv:
    sid = sys.argv[sys.argv.index("--pi-session-id") + 1]
    sd = pathlib.Path(sys.argv[sys.argv.index("--pi-session-dir") + 1])
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"2026-01-01T00-00-00-000Z_{sid}.jsonl").touch()
state = os.environ.get("FAKE_STATE")
if state and "--state-file" in sys.argv:
    open(sys.argv[sys.argv.index("--state-file") + 1], "w").write(state)
if os.environ.get("FAKE_MODE", "done") == "done":
    m = re.search(r'"([^"]+)" "([^"]+)" --set "([^"]+)" done --handover-dir "([^"]+)"',
                  sys.argv[-1])
    subprocess.run([m.group(1), m.group(2), "--set", m.group(3), "done",
                    "--handover-dir", m.group(4)], check=True)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


def _fake_harness(root: Path) -> Path:
    path = root / "fake_harness.py"
    path.write_text(FAKE_HARNESS)
    path.chmod(0o755)
    return path


def _handover(root: Path, entries: list[dict], progress: bool = True) -> Path:
    hd = root / "handover"
    hd.mkdir()
    (hd / "goal.md").write_text("## Goal\nDo it\n")
    for entry in entries:
        body = "---\nstatus: %s\n---\n## Objective\nDo it\n" % entry.get("status", "pending")
        if progress:
            body += "\n## Progress / Handover\n"
        (hd / entry["file"]).write_text(body)
    (hd / "plans.json").write_text(json.dumps(entries))
    status.init(hd)
    return hd


def _run_fanout(hd: Path, harness: Path, argv_dir: Path, *extra: str,
                env: dict | None = None, mode: str = "done",
                exit_code: str = "0", state: str | None = None):
    child_env = {**os.environ, "ARGV_DIR": str(argv_dir), "FAKE_MODE": mode,
                 "FAKE_EXIT": exit_code, **(env or {})}
    if state:
        child_env["FAKE_STATE"] = state
    return subprocess.run(
        [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", str(hd),
         "--harness", str(harness), "--stopwatch", "5", "--grace", "1",
         "--slack", "1", *extra],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=child_env, check=False)


def _argvs(argv_dir: Path) -> list[list[str]]:
    return [json.loads(p.read_text()) for p in sorted(argv_dir.iterdir())]



class BackendCliContractTests(unittest.TestCase):
    def test_every_entry_point_rejects_an_unknown_backend(self) -> None:
        runs = {
            "harness": [HARNESS, "--dry-run", "--backend", "bogus", "--", "hi"],
            "fanout": [PYTHON, str(WF / "casper_fanout.py"), "--handover-dir", ".",
                       "--backend", "bogus"],
            "verify": [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", ".",
                       "--backend", "bogus"],
        }
        for name, cmd in runs.items():
            with self.subTest(entry_point=name):
                run = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, check=False)
                self.assertEqual(run.returncode, 2, run.stdout)

    def test_explicit_backend_overrides_the_driver_rule_in_both_directions(self) -> None:
        cases = (
            # backend, model, env, expected fragment, forbidden fragment
            ("claude", "opus", GPT_DRIVER_ENV,
             "claude -p", "pi --mode rpc"),
            ("claude", "anthropic/claude-opus-5", GPT_DRIVER_ENV,
             "--model claude-opus-5", "anthropic/"),
            ("pi", "opus", {}, "--model anthropic/claude-opus-5", "claude -p"),
            ("pi", "gpt-5.6-sol", {}, "--provider openai", "claude -p"),
            ("pi", "openai-codex/gpt-5.6-sol", {},
             "--model openai-codex/gpt-5.6-sol", "--provider"),
            ("auto", "opus", {}, "claude -p", "pi --mode rpc"),
        )
        for backend, model, env, expected, forbidden in cases:
            with self.subTest(backend=backend, model=model):
                run = subprocess.run(
                    [HARNESS, "--dry-run", "--backend", backend, "--model", model,
                     "--", "hi"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, **env}, check=False)
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertIn(expected, run.stdout)
                self.assertNotIn(forbidden, run.stdout)

    def test_explicit_claude_refuses_a_model_it_cannot_run(self) -> None:
        for model in ("gpt-5", "openai-codex/gpt-5.6-sol", "claude/claude-opus-5",
                      "anthropic/gpt-5", "CLAUDE-opus-4-7"):
            with self.subTest(model=model):
                run = subprocess.run(
                    [HARNESS, "--dry-run", "--backend", "claude", "--model", model,
                     "--", "hi"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False)
                self.assertEqual(run.returncode, 2, run.stdout)
                self.assertIn("cannot run model", run.stdout)
                self.assertFalse(fanout._claude_accepts(model))

    def test_claude_backend_with_a_gpt_driver_default_asks_for_a_model(self) -> None:
        # No --model under a GPT pi driver: the derived default is the driver's own
        # GPT id. Explicit --backend claude must ask the user for a Claude model
        # instead of quietly running a different model or a different CLI.
        run = subprocess.run(
            [HARNESS, "--dry-run", "--backend", "claude", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, **GPT_DRIVER_ENV}, check=False)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("openai-codex/gpt-6-astra", run.stdout)
        self.assertIn("derived default model", run.stdout)
        self.assertIn("--model", run.stdout)

    def test_token_cap_follows_the_effective_backend_only(self) -> None:
        # claude backend: the cap reaches casper_guard.py even under a pi driver.
        guarded = subprocess.run(
            [HARNESS, "--dry-run", "--backend", "claude", "--model", "opus",
             "--max-context-tokens", "470000", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, **GPT_DRIVER_ENV}, check=False)
        self.assertEqual(guarded.returncode, 0, guarded.stdout)
        self.assertIn("casper_guard.py", guarded.stdout)
        self.assertIn("--max-context-tokens 470000", guarded.stdout)
        # pi backend: an explicit nonzero cap is refused, an explicit 0 is fine.
        refused = subprocess.run(
            [HARNESS, "--dry-run", "--backend", "pi", "--model", "opus",
             "--max-context-tokens", "470000", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn("needs the claude backend", refused.stdout)
        zero = subprocess.run(
            [HARNESS, "--dry-run", "--backend", "pi", "--model", "opus",
             "--max-context-tokens", "0", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(zero.returncode, 0, zero.stdout)
        self.assertIn("casper_pi_guard.py", zero.stdout)

    def test_pi_stall_zero_keeps_the_plain_print_route_on_an_explicit_backend(self) -> None:
        run = subprocess.run(
            [HARNESS, "--dry-run", "--backend", "pi", "--model", "opus",
             "--pi-stall-secs", "0", "--", "hi"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("pi -p", run.stdout)
        self.assertIn("--model anthropic/claude-opus-5", run.stdout)
        self.assertNotIn("casper_pi_guard.py", run.stdout)
        self.assertNotIn("casper_guard.py", run.stdout)


class FanoutBackendTests(unittest.TestCase):
    def test_backend_precedence_is_plan_then_cli_then_auto(self) -> None:
        # Backend precedence mirrors the model's but is decided independently:
        # the per-plan model here is a GPT and the per-plan backend still wins.
        cases = (
            # plan backend, CLI backend, expected forwarded backend
            ("claude", "pi", "claude"),
            (None, "pi", "pi"),
            ("pi", None, "pi"),
            (None, None, "auto"),
        )
        for plan_backend, cli_backend, expected in cases:
            with self.subTest(plan=plan_backend, cli=cli_backend):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    entry = {"file": "plan.md", "title": "one", "wave": 0,
                             "model": "opus", "effort": "low"}
                    if plan_backend:
                        entry["backend"] = plan_backend
                    hd = _handover(root, [entry])
                    argv_dir = root / "argv"
                    extra = ["--backend", cli_backend] if cli_backend else []
                    run = _run_fanout(hd, _fake_harness(root), argv_dir, *extra)
                    self.assertEqual(run.returncode, 0, run.stdout)
                    argv = _argvs(argv_dir)[0]
                    self.assertEqual(argv[argv.index("--backend") + 1], expected)
                    # model and effort keep their own precedence
                    self.assertEqual(argv[argv.index("-m") + 1], "opus")
                    self.assertEqual(argv[argv.index("-t") + 1], "low")

    def test_claude_backend_resolver_under_a_gpt_pi_driver(self) -> None:
        # Driver's own model is GPT-6-astra on a pi profile; the plan pins Opus on
        # the claude backend. Fanout must forward that pair, keep the claude-only
        # plumbing, and the harness must route the same pair to the Claude CLI.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{"file": "plan.md", "title": "one", "wave": 0,
                                   "model": "opus", "backend": "claude"}])
            argv_dir = root / "argv"
            run = _run_fanout(hd, _fake_harness(root), argv_dir, env=GPT_DRIVER_ENV)
            self.assertEqual(run.returncode, 0, run.stdout)
            argv = _argvs(argv_dir)[0]
            self.assertEqual(argv[argv.index("--backend") + 1], "claude")
            self.assertEqual(argv[argv.index("-m") + 1], "opus")
            self.assertEqual(argv[argv.index("--max-context-tokens") + 1], "470000")
            self.assertIn("--state-file", argv)
            self.assertNotIn("--pi-session-id", argv)
            self.assertIn("context budget", argv[-1])
            self.assertIn("backend claude", (hd / "logs" / "plan.log").read_text())
            # parity: the same (backend, model) pair reaches the Claude CLI
            dry = subprocess.run(
                [HARNESS, "--dry-run", "--backend", "claude", "--model", "opus",
                 "--max-context-tokens", "470000", "--", "hi"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, **GPT_DRIVER_ENV}, check=False)
            self.assertEqual(dry.returncode, 0, dry.stdout)
            self.assertIn("casper_guard.py", dry.stdout)
            self.assertIn("--model claude-opus-5", dry.stdout)
            self.assertTrue(fanout._is_claude_model("opus", "claude"))

    def test_invalid_pair_fails_before_any_lease_or_plan_edit(self) -> None:
        # A mixed batch: one dispatchable pi plan and one impossible pair. The
        # whole run must fail before claiming, editing or logging anything.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [
                {"file": "plan-a.md", "title": "a", "wave": 0,
                 "model": "openai-codex/gpt-5.6-sol"},
                {"file": "plan-b.md", "title": "b", "wave": 0,
                 "model": "openai-codex/gpt-5.6-sol", "backend": "claude"},
            ], progress=False)
            before_ledger = (hd / "plans.json").read_bytes()
            before_plans = {name: (hd / name).read_bytes()
                            for name in ("plan-a.md", "plan-b.md")}
            argv_dir = root / "argv"
            argv_dir.mkdir()
            run = _run_fanout(hd, _fake_harness(root), argv_dir)
            self.assertEqual(run.returncode, 2, run.stdout)
            self.assertIn("plan-b.md", run.stdout)
            self.assertNotIn("plan-a.md", run.stdout)
            self.assertIn("claude* or anthropic/claude*", run.stdout)
            self.assertEqual((hd / "plans.json").read_bytes(), before_ledger)
            for name, body in before_plans.items():
                self.assertEqual((hd / name).read_bytes(), body)
            self.assertEqual(list(argv_dir.iterdir()), [])  # no resolver launched
            self.assertFalse((hd / "fanout-result.json").exists())

    def test_invalid_per_plan_backend_value_names_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{"file": "plan.md", "title": "one", "wave": 0,
                                   "backend": "Claude Code"}])
            before = (hd / "plans.json").read_bytes()
            argv_dir = root / "argv"
            argv_dir.mkdir()
            run = _run_fanout(hd, _fake_harness(root), argv_dir)
            self.assertEqual(run.returncode, 2, run.stdout)
            self.assertIn("plan.md: backend must be one of auto|pi|claude", run.stdout)
            self.assertEqual((hd / "plans.json").read_bytes(), before)

    def test_explicit_cap_on_a_pi_plan_fails_instead_of_being_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [
                {"file": "plan-a.md", "title": "a", "wave": 0, "model": "opus",
                 "backend": "claude"},
                {"file": "plan-b.md", "title": "b", "wave": 0, "model": "opus",
                 "backend": "pi"},
            ])
            before = (hd / "plans.json").read_bytes()
            argv_dir = root / "argv"
            argv_dir.mkdir()
            run = _run_fanout(hd, _fake_harness(root), argv_dir,
                              "--max-context-tokens", "200000")
            self.assertEqual(run.returncode, 2, run.stdout)
            self.assertIn("plan-b.md", run.stdout)
            self.assertIn("needs the claude backend", run.stdout)
            self.assertEqual((hd / "plans.json").read_bytes(), before)
            self.assertEqual(list(argv_dir.iterdir()), [])

    def test_omitted_cap_is_claude_only_and_explicit_zero_works_on_both(self) -> None:
        for extra, claude_ctx, pi_ctx in ((["--max-context-tokens", "0"], "0", "0"),
                                          ([], "470000", "0")):
            with self.subTest(flag=extra or "omitted"):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    hd = _handover(root, [
                        {"file": "plan-a.md", "title": "a", "wave": 0,
                         "model": "opus", "backend": "claude"},
                        {"file": "plan-b.md", "title": "b", "wave": 0,
                         "model": "opus", "backend": "pi"},
                    ])
                    argv_dir = root / "argv"
                    run = _run_fanout(hd, _fake_harness(root), argv_dir, *extra)
                    self.assertEqual(run.returncode, 0, run.stdout)
                    seen = {}
                    for argv in _argvs(argv_dir):
                        plan = "plan-a.md" if "plan-a.md" in argv[-1] else "plan-b.md"
                        seen[plan] = argv
                    self.assertEqual(
                        seen["plan-a.md"][seen["plan-a.md"].index("--max-context-tokens") + 1],
                        claude_ctx)
                    self.assertEqual(
                        seen["plan-b.md"][seen["plan-b.md"].index("--max-context-tokens") + 1],
                        pi_ctx)
                    self.assertIn("--state-file", seen["plan-a.md"])
                    self.assertIn("--pi-session-id", seen["plan-b.md"])


class SessionProvenanceTests(unittest.TestCase):
    STATE = json.dumps({"session": "sess-claude-1", "reason": "child-exit", "gauge": 100})

    def test_legacy_untagged_session_cold_starts_once_then_is_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "model": "opus", "backend": "claude",
                "session": "legacy-untagged", "pause_reason": "child-exit"}])
            argv_dir = root / "argv"
            harness = _fake_harness(root)
            # Round 1: an id without provenance is not resumed — cold start.
            run = _run_fanout(hd, harness, argv_dir, mode="pause", state=self.STATE)
            self.assertEqual(run.returncode, 1, run.stdout)
            argv = _argvs(argv_dir)[-1]
            self.assertNotIn("--resume-session", argv)
            self.assertIn("Resolve the whole approved work unit", argv[-1])
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual((entry["session"], entry["session_backend"]),
                             ("sess-claude-1", "claude"))
            # Round 2: the freshly tagged session warm-resumes on the same backend.
            run = _run_fanout(hd, harness, argv_dir, mode="pause", state=self.STATE)
            self.assertEqual(run.returncode, 1, run.stdout)
            argv = _argvs(argv_dir)[-1]
            self.assertEqual(argv[argv.index("--resume-session") + 1], "sess-claude-1")
            self.assertIn("RESUMING", argv[-1])

    def test_a_session_is_never_resumed_on_the_other_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "model": "opus", "session": "pi-uuid-1", "session_backend": "pi",
                "pause_reason": "child-exit"}])
            (hd / "pi-sessions").mkdir()
            (hd / "pi-sessions" / "2026-01-01T00-00-00-000Z_pi-uuid-1.jsonl").touch()
            argv_dir = root / "argv"
            harness = _fake_harness(root)
            # A pi id must never reach `claude --resume`.
            run = _run_fanout(hd, harness, argv_dir, "--backend", "claude",
                              mode="pause", state=self.STATE)
            self.assertEqual(run.returncode, 1, run.stdout)
            argv = _argvs(argv_dir)[-1]
            self.assertNotIn("--resume-session", argv)
            self.assertIn("Resolve the whole approved work unit", argv[-1])
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual(entry["session_backend"], "claude")
            # ...and a claude id must never be handed to pi as a session id.
            run = _run_fanout(hd, harness, argv_dir, "--backend", "pi", mode="pause")
            self.assertEqual(run.returncode, 1, run.stdout)
            argv = _argvs(argv_dir)[-1]
            self.assertNotEqual(argv[argv.index("--pi-session-id") + 1], "sess-claude-1")
            self.assertIn("Resolve the whole approved work unit", argv[-1])

    def test_same_backend_warm_resume_and_needs_user_pause_keep_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "model": "opus", "backend": "claude", "session": "sess-claude-1",
                "session_backend": "claude", "pause_reason": "child-exit"}])
            argv_dir = root / "argv"
            harness = _fake_harness(root)
            run = _run_fanout(hd, harness, argv_dir, mode="pause", state=self.STATE)
            self.assertEqual(run.returncode, 1, run.stdout)
            argv = _argvs(argv_dir)[-1]
            self.assertEqual(argv[argv.index("--resume-session") + 1], "sess-claude-1")
            # A bare NEEDS-USER pause (no session arguments) preserves both fields.
            (hd / "plan.md").write_text(
                "---\nstatus: paused\n---\n## Objective\nDo it\n\n"
                "## Progress / Handover\nNEEDS-USER: which option?\n")
            run = _run_fanout(hd, harness, argv_dir, mode="pause", state=self.STATE)
            self.assertEqual(run.returncode, 1, run.stdout)
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual((entry["session"], entry["session_backend"]),
                             ("sess-claude-1", "claude"))

    def test_manifest_reinit_drops_an_id_together_with_its_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = _handover(root, [{
                "file": "plan.md", "title": "one", "wave": 0, "status": "paused",
                "session": "sess-claude-1", "session_backend": "claude"}])
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([{"file": "plan.md", "title": "one", "wave": 0}]))
            status.init(hd, manifest)
            entry = status._find(status._load_raw(hd), "plan.md")
            self.assertEqual(entry["status"], "paused")  # live status is kept
            self.assertIsNone(entry["session"])
            self.assertIsNone(entry["session_backend"])


class VerifierBackendTests(unittest.TestCase):
    GOAL = """## Goal
Review.
## Acceptance Criteria
1. Clear.
## Verification
1. judgment: Check clarity.
"""

    def _judge_harness(self, root: Path, argv_dump: Path) -> Path:
        path = root / "judge.py"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json,pathlib,sys\n"
            f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))\n"
            "print(json.dumps([{'index':0,'status':'pass','evidence':'ok'}]).replace(\"'\",'\\\"'))\n"
        )
        path.chmod(0o755)
        return path

    def _verify(self, backend: str | None) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hd = root / "handover"
            hd.mkdir()
            (hd / "goal.md").write_text(self.GOAL)
            (hd / "plan.md").write_text("---\nstatus: done\n---\n")
            (hd / "plans.json").write_text(json.dumps(
                [{"file": "plan.md", "title": "one", "wave": 0, "status": "done"}]))
            status.init(hd)
            argv_dump = root / "argv.json"
            cmd = [PYTHON, str(WF / "casper_verify.py"), "--handover-dir", str(hd),
                   "--harness", str(self._judge_harness(root, argv_dump)),
                   "--judgment-stopwatch", "5"]
            if backend:
                cmd += ["--backend", backend]
            run = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            return json.loads(argv_dump.read_text())

    def test_backend_is_forwarded_only_when_it_is_not_the_default(self) -> None:
        for backend in ("claude", "pi"):
            with self.subTest(backend=backend):
                argv = self._verify(backend)
                self.assertEqual(argv[argv.index("--backend") + 1], backend)
        for backend in (None, "auto"):
            with self.subTest(backend=backend):
                self.assertNotIn("--backend", self._verify(backend))


if __name__ == "__main__":
    unittest.main()
