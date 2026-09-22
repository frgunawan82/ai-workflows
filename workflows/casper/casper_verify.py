#!/usr/bin/env python3
"""Verify Casper acceptance criteria with commands first and at most one LLM call.

`goal.md` contract:

    ## Acceptance Criteria
    1. The focused tests pass.
    2. The result is easy to understand.

    ## Verification
    1. command (timeout=120)
       ```bash
       python -m unittest tests.test_feature
       ```
    2. judgment: Inspect the changed result for clarity and consistency.

Command criteria run locally and are decided only by exit status. All judgment
criteria, when present, are sent in one batch to LLM_harness.sh. The latest
`verify.json` is always replaced, so stale passing evidence cannot authorize cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import casper_status as cs

HERE = Path(__file__).resolve().parent
DEFAULT_HARNESS = HERE / "LLM_harness.sh"


@dataclass(frozen=True)
class Check:
    criterion: str
    kind: str
    body: str
    timeout: int


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)", text)
    if not match:
        raise ValueError(f"goal.md lacks ## {heading}")
    return match.group(1)


def _numbered(section: str) -> list[str]:
    items = re.findall(r"(?ms)^\s*\d+\.\s+(.*?)(?=^\s*\d+\.\s+|\Z)", section)
    return [item.strip() for item in items]


def parse_goal(goal_path: Path, default_timeout: int = 300) -> list[Check]:
    text = goal_path.read_text()
    criteria = _numbered(_section(text, "Acceptance Criteria"))
    methods = _numbered(_section(text, "Verification"))
    if not criteria:
        raise ValueError("## Acceptance Criteria must contain a numbered list")
    if len(criteria) != len(methods):
        raise ValueError("Acceptance Criteria and Verification counts differ")

    checks: list[Check] = []
    label_re = re.compile(r"(?is)^\s*(?:\[(command|judgment)\]|(command|judgment)\b)")
    for criterion, method in zip(criteria, methods):
        first = method.splitlines()[0].strip()
        timeout_match = re.search(r"timeout\s*[:= ]\s*(\d+)", first, re.IGNORECASE)
        timeout = int(timeout_match.group(1)) if timeout_match else default_timeout
        label = label_re.match(method)
        if not label:
            raise ValueError(f"verification item must start with command or judgment: {first}")
        kind = (label.group(1) or label.group(2)).lower()
        remainder = method[label.end():]
        if kind == "command":
            fence = re.search(r"(?ms)```(?:bash|sh)?\s*\n(.*?)```", remainder)
            if fence:
                body = fence.group(1).strip()
            else:
                body = re.sub(r"(?is)^\s*(?:\([^)]*\))?\s*[:—-]?\s*", "", remainder)
                body = body.strip().strip("`")
        else:
            body = re.sub(r"(?is)^\s*[:—-]?\s*", "", remainder).strip()
        if not body:
            raise ValueError(f"empty {kind} verification for: {criterion}")
        checks.append(Check(criterion=criterion, kind=kind, body=body, timeout=timeout))
    return checks


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n... [{len(text) - limit} chars truncated] ...\n"
    if limit <= len(marker):
        return marker[:limit]
    room = limit - len(marker)
    head = room // 2
    return text[:head] + marker + text[-(room - head):]


class _BoundedCapture:
    """Drain arbitrary child output while retaining only bounded head/tail bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_room = self.head_limit - len(self.head)
        if head_room > 0:
            self.head.extend(chunk[:head_room])
            chunk = chunk[head_room:]
        if chunk and self.tail_limit:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def text(self) -> str:
        if self.total <= self.limit:
            raw = bytes(self.head + self.tail)
        else:
            omitted = self.total - len(self.head) - len(self.tail)
            marker = f"\n... [{omitted} output bytes truncated] ...\n".encode()
            raw = bytes(self.head) + marker + bytes(self.tail)
        return raw.decode(errors="replace")


def _collect_command(proc: subprocess.Popen[bytes], timeout: float,
                     capture: _BoundedCapture) -> bool:
    """Drain stdout, terminating the whole process group after the timeout."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    term_deadline: float | None = None
    timed_out = False
    eof = False
    try:
        while True:
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    capture.add(chunk)
                else:
                    eof = True
                    selector.unregister(key.fd)

            now = time.monotonic()
            if proc.poll() is None and not timed_out and now >= deadline:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                term_deadline = now + 5
            elif (proc.poll() is None and timed_out and term_deadline is not None
                  and now >= term_deadline):
                os.killpg(proc.pid, signal.SIGKILL)
                term_deadline = None

            if proc.poll() is not None:
                # Drain bytes already buffered in the pipe without waiting on a
                # descendant that improperly kept the descriptor open.
                while not eof:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        eof = True
                        break
                    capture.add(chunk)
                break
    finally:
        selector.close()
        proc.stdout.close()
    proc.wait()
    return timed_out


def run_command(check: Check, cwd: Path, evidence_chars: int) -> dict:
    proc = subprocess.Popen(
        check.body,
        cwd=cwd,
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    capture = _BoundedCapture(max(evidence_chars * 2, 1024))
    timed_out = _collect_command(proc, check.timeout, capture)
    output = capture.text()
    exit_label = "timeout" if timed_out else str(proc.returncode)
    evidence = truncate(
        f"command: {check.body}\nexit: {exit_label}\noutput:\n{output or '(no output)'}",
        evidence_chars,
    )
    return {
        "criterion": check.criterion,
        "status": "pass" if not timed_out and proc.returncode == 0 else "fail",
        "evidence": evidence,
    }


def _json_array(text: str) -> list[dict]:
    # The harness stdout can wrap the model's answer in prose/ANSI noise that
    # itself contains "[" / "]" (citations, escape codes), so a naive
    # first-[ .. last-] span is not reliably JSON. Scan every "[" and keep the
    # LAST decodable JSON array of objects — the model's final answer.
    decoder = json.JSONDecoder()
    found = None
    idx = text.find("[")
    while idx >= 0:
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except ValueError:
            pass
        else:
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                found = value
        idx = text.find("[", idx + 1)
    if found is None:
        raise ValueError("judgment resolver returned no JSON array of objects")
    return found


def run_judgments(
    checks: list[tuple[int, Check]], handover_dir: Path, harness: Path,
    model: str | None, effort: str | None, stopwatch: int, evidence_chars: int,
    backend: str = "auto",
) -> dict[int, dict]:
    payload = [{"index": index, "criterion": check.criterion, "instruction": check.body}
               for index, check in checks]
    prompt = (
        f"Read @{handover_dir / 'goal.md'} and inspect the relevant work/artifacts. "
        "Judge ALL subjective criteria below in this one batch. Do not modify anything. "
        "Return ONLY a JSON array with one object per input, preserving index: "
        '[{"index":0,"status":"pass|fail","evidence":"brief cited evidence"}].\n'
        + json.dumps(payload, indent=2)
    )
    cmd = [str(harness)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-t", effort]
    # Only a non-default backend is forwarded: "auto" IS the harness default, so
    # omitting it keeps the judgment command line unchanged for existing callers.
    if backend and backend != "auto":
        cmd += ["--backend", backend]
    cmd += ["-s", str(stopwatch), "--", prompt]
    try:
        run = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=stopwatch + 20, check=False)
        if run.returncode != 0:
            raise ValueError(f"judgment harness exit {run.returncode}: {run.stdout}")
        rows = _json_array(run.stdout)
        by_index = {int(row["index"]): row for row in rows}
        results = {}
        for index, check in checks:
            row = by_index[index]
            status = row.get("status")
            if status not in ("pass", "fail"):
                raise ValueError(f"invalid judgment status for index {index}: {status}")
            results[index] = {
                "criterion": check.criterion,
                "status": status,
                "evidence": truncate(str(row.get("evidence", "")), evidence_chars),
            }
        return results
    except (OSError, KeyError, ValueError, subprocess.TimeoutExpired) as exc:
        evidence = truncate(f"batched judgment failed: {exc}", evidence_chars)
        return {index: {"criterion": check.criterion, "status": "fail", "evidence": evidence}
                for index, check in checks}


def _write_results(path: Path, results: list[dict]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2) + "\n")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic-first Casper acceptance verifier.")
    ap.add_argument("--handover-dir", required=True, type=Path)
    ap.add_argument("--cwd", type=Path, default=Path.cwd(),
                    help="Working directory for acceptance commands")
    ap.add_argument("--command-timeout", type=int, default=300)
    ap.add_argument("--evidence-chars", type=int, default=3000)
    ap.add_argument("--harness", type=Path, default=DEFAULT_HARNESS)
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--backend", choices=("auto", "pi", "claude"), default="auto",
                    help="Backend for the batched judgment call, forwarded to "
                         "LLM_harness.sh (auto: the harness's model/driver rule)")
    ap.add_argument("--judgment-stopwatch", type=int, default=1800)
    args = ap.parse_args()

    hd = args.handover_dir.resolve()
    output = hd / "verify.json"
    try:
        unresolved = cs.list_unresolved(hd)
        if unresolved:
            raise ValueError(f"plans are not done: {', '.join(unresolved)}")
        checks = parse_goal(hd / "goal.md", args.command_timeout)
        results: list[dict | None] = [None] * len(checks)
        judgments = []
        for index, check in enumerate(checks):
            if check.kind == "command":
                results[index] = run_command(check, args.cwd, args.evidence_chars)
            else:
                judgments.append((index, check))
        if judgments:
            for index, result in run_judgments(
                judgments, hd, args.harness, args.model, args.effort,
                args.judgment_stopwatch, args.evidence_chars, args.backend,
            ).items():
                results[index] = result
        final = [result for result in results if result is not None]
    except (OSError, ValueError) as exc:
        final = [{"criterion": "verification contract", "status": "fail", "evidence": str(exc)}]

    _write_results(output, final)
    passed = bool(final) and all(row["status"] == "pass" for row in final)
    print(f"{len(final)} criterion result(s) -> {output}; {'ALL PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
