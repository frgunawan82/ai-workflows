#!/usr/bin/env python3
"""Atomic status ledger for casper plan documents — the resumability backbone.

The ledger is `<handover-dir>/plans.json`: a JSON array of plan entries, the SAME
file the planner emits as a manifest, augmented in place with status + lease so any
headless LLM (or a fresh session) can tell what is resolved and resume only the rest.

Entry shape:
  {"file","title","wave","model","effort","backend","status","lease","updated_at",
   "session","session_backend","pause_reason","zero_progress"}
  status        : pending | in_progress | done | paused | failed
  lease         : null | {"pid","host","started_at"(epoch),"stale_secs"} (in_progress owner)
  backend       : null | auto | pi | claude (optional per-plan backend for fanout)
  session       : null | session id recorded on a warm-resumable pause; the next dispatch
                  re-enters that session (claude -p --resume / pi --session) instead of a
                  cold start
  session_backend : null | claude | pi — which CLI minted `session`. A claude session id and
                  a pi session id are not interchangeable, so a dispatch only warm-resumes a
                  session tagged with the backend it is about to run; anything else (including
                  a legacy untagged entry) cold-starts. Written/cleared with `session`.
  pause_reason  : null | token | time | signal | child-exit | provider-limit (why the last run paused)
  zero_progress : consecutive runs the token budget killed on the very first model call
                  (plan input may not fit the budget); fanout flags NEEDS-USER at the limit

This module is BOTH a CLI and the shared library that casper_fanout.py imports
(init / list_open / claim / set_status). It is the only writer of the ledger; every
write is atomic (temp + os.replace) under an fcntl lock on a sidecar `.lock`, so
concurrent resolvers/fan-outs never corrupt it or double-claim a plan.

Exit codes:
  0  ok / healthy
  1  refused or unhealthy (--scaffold without goal.md; --health check failed
     or nothing to check)
  2  usage error
  3  plan not found in ledger
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("pending", "in_progress", "done", "paused", "failed")
OPEN_STATUSES = ("pending", "paused", "failed")
LEDGER_NAME = "plans.json"
DEFAULT_STALE_SECS = 7800  # stopwatch(7200) + grace(300) + slack(300)

PLAN_TITLE = "Complete the approved goal"
_PLAN_TEMPLATE = """---
status: pending
---

## Objective
<complete the approved goal; include relevant paths, constraints, and focused checks>

## Progress / Handover
"""
# Mirror casper_fanout's checkpoint markers (fanout imports this module, so the
# constants live here duplicated rather than creating a circular import).
_PROGRESS_RE = re.compile(r"(?ms)^## Progress / Handover\s*\n(.*?)(?=^##\s|\Z)")
_NEEDS_USER_PREFIX = "NEEDS-USER: "


# --------------------------------------------------------------------------- io

def ledger_path(handover_dir: Path) -> Path:
    return Path(handover_dir) / LEDGER_NAME


@contextlib.contextmanager
def _locked(handover_dir: Path):
    """Exclusive lock on a sidecar file, held for a read-modify-write cycle."""
    Path(handover_dir).mkdir(parents=True, exist_ok=True)
    lock = ledger_path(handover_dir).with_suffix(".json.lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_raw(handover_dir: Path) -> list[dict]:
    p = ledger_path(handover_dir)
    if not p.exists():
        return []
    data = json.loads(p.read_text() or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{p} must contain a JSON array")
    return data


def _save_raw(handover_dir: Path, plans: list[dict]) -> None:
    p = ledger_path(handover_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plans, indent=2) + "\n")
    os.replace(tmp, p)  # atomic within the same directory


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(entry: dict) -> dict:
    """Ensure a manifest entry has every ledger field (preserving any present)."""
    f = entry.get("file")
    if not f:
        raise ValueError(f"plan entry missing 'file': {entry!r}")
    return {
        "file": f,
        "title": entry.get("title") or f,
        "wave": int(entry.get("wave", 0) or 0),
        "model": entry.get("model"),
        "effort": entry.get("effort"),
        "backend": entry.get("backend"),
        "status": entry.get("status") or "pending",
        "lease": entry.get("lease"),
        "updated_at": entry.get("updated_at") or _now_iso(),
        "session": entry.get("session"),
        # Provenance travels with the id it describes: a re-init from a manifest
        # that carries neither drops both, so no untagged id can be resumed.
        "session_backend": entry.get("session_backend"),
        "pause_reason": entry.get("pause_reason"),
        "zero_progress": int(entry.get("zero_progress") or 0),
    }


# ---------------------------------------------------------------- frontmatter

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def stamp_frontmatter(plan_file: Path, status: str) -> None:
    """Mirror `status:` into the plan doc's YAML frontmatter (best-effort)."""
    try:
        if not plan_file.exists():
            return
        text = plan_file.read_text()
        m = _FM_RE.match(text)
        if m:
            block = m.group(1)
            if re.search(r"(?m)^status:.*$", block):
                block = re.sub(r"(?m)^status:.*$", f"status: {status}", block)
            else:
                block = f"status: {status}\n{block}"
            new = f"---\n{block}\n---\n" + text[m.end():]
        else:
            new = f"---\nstatus: {status}\n---\n{text}"
        if new != text:
            tmp = plan_file.with_suffix(plan_file.suffix + ".tmp")
            tmp.write_text(new)
            os.replace(tmp, plan_file)
    except OSError:
        pass  # frontmatter is a mirror; the ledger stays authoritative


# ----------------------------------------------------------------- predicates

def is_open(entry: dict, stale_secs: int, now: float | None = None) -> bool:
    """Open = needs work: pending/paused/failed, or an in_progress with a dead lease."""
    now = time.time() if now is None else now
    st = entry.get("status", "pending")
    if st in OPEN_STATUSES:
        return True
    if st == "in_progress":
        lease = entry.get("lease") or {}
        started = lease.get("started_at")
        # A caller may use a shorter default than the process that owns this
        # lease. Never reclaim before the duration recorded by the claimant.
        effective_stale_secs = max(stale_secs, int(lease.get("stale_secs") or 0))
        return started is None or (now - float(started)) > effective_stale_secs
    return False  # done


def _find(plans: list[dict], plan_file: str) -> dict | None:
    name = Path(plan_file).name
    for e in plans:
        if e.get("file") == name:
            return e
    return None


# --------------------------------------------------------------- library API

def init(handover_dir: Path, manifest: Path | None = None) -> int:
    """Seed/merge the ledger from a manifest; preserve status/lease of known plans."""
    handover_dir = Path(handover_dir)
    with _locked(handover_dir):
        src = manifest if manifest is not None else ledger_path(handover_dir)
        raw = json.loads(Path(src).read_text() or "[]")
        if not isinstance(raw, list):
            raise ValueError(f"{src} must contain a JSON array")
        existing = {e["file"]: e for e in _load_raw(handover_dir) if e.get("file")}
        merged = []
        for item in raw:
            norm = _normalize(item)
            prev = existing.get(norm["file"])
            if prev:  # keep live status/lease, refresh metadata from manifest
                norm["status"] = prev.get("status", norm["status"])
                norm["lease"] = prev.get("lease")
                norm["updated_at"] = prev.get("updated_at", norm["updated_at"])
            merged.append(norm)
            stamp_frontmatter(handover_dir / norm["file"], norm["status"])
        _save_raw(handover_dir, merged)
        return len(merged)


def list_open(handover_dir: Path, stale_secs: int = DEFAULT_STALE_SECS) -> list[str]:
    """Return dispatchable plans (including expired, but not live, leases)."""
    plans = _load_raw(Path(handover_dir))
    return [e["file"] for e in plans if is_open(e, stale_secs)]


def list_unresolved(handover_dir: Path) -> list[str]:
    """Return every plan not done, including plans with a live lease."""
    plans = _load_raw(Path(handover_dir))
    return [e["file"] for e in plans if e.get("status", "pending") != "done"]


def claim(handover_dir: Path, plan_file: str, stale_secs: int = DEFAULT_STALE_SECS) -> str:
    """Atomically take an open plan -> in_progress with a lease. Returns claimed|skip|not_found."""
    handover_dir = Path(handover_dir)
    with _locked(handover_dir):
        plans = _load_raw(handover_dir)
        e = _find(plans, plan_file)
        if e is None:
            return "not_found"
        if not is_open(e, stale_secs):
            return "skip"  # done, or a live lease holds it
        e["status"] = "in_progress"
        e["lease"] = {"pid": os.getpid(), "host": socket.gethostname(),
                      "started_at": time.time(), "stale_secs": stale_secs}
        e["updated_at"] = _now_iso()
        _save_raw(handover_dir, plans)
        stamp_frontmatter(handover_dir / e["file"], "in_progress")
        return "claimed"


def scaffold(handover_dir: Path) -> dict:
    """Create the default whole-goal plan doc + manifest entry, then seed the ledger.

    Idempotent: an existing plan doc or manifest entry is kept untouched, so a
    re-run after editing `## Objective` never loses work. Requires goal.md —
    a plan ledger only exists for an approved goal.
    """
    handover_dir = Path(handover_dir)
    if not (handover_dir / "goal.md").exists():
        raise FileNotFoundError(
            f"no goal.md in {handover_dir} — scaffold only an approved goal")
    plan_name = f"plan-01-{handover_dir.resolve().name}.md"
    plan_path = handover_dir / plan_name
    created_plan = not plan_path.exists()
    if created_plan:
        plan_path.write_text(_PLAN_TEMPLATE)
    with _locked(handover_dir):
        plans = _load_raw(handover_dir)
        created_entry = _find(plans, plan_name) is None
        if created_entry:
            plans.append({"file": plan_name, "title": PLAN_TITLE, "wave": 0})
            _save_raw(handover_dir, plans)
    seeded = init(handover_dir)
    return {"plan": plan_name, "created_plan": created_plan,
            "created_entry": created_entry, "seeded": seeded}


def health(handover_dir: Path) -> tuple[int, list[str]]:
    """Read-only completion audit: plans all done, no open NEEDS-USER, verify all-pass.

    Returns (exit_code, report_lines): 0 healthy, 1 a check failed or there is
    nothing to check (no handover dir). Never writes — safe on a live handover.
    """
    hd = Path(handover_dir)
    if not hd.is_dir():
        return 1, [f"no handover dir at {hd} — nothing to check "
                   "(never started, or already cleaned up)"]
    lines: list[str] = []
    ok = True

    if (hd / "goal.md").exists():
        lines.append("ok    goal.md: present")
    else:
        ok = False
        lines.append("FAIL  goal.md: missing — not a casper handover dir?")

    plans: list[dict] = []
    if not ledger_path(hd).exists():
        ok = False
        lines.append("FAIL  plans: no ledger — nothing was dispatched yet")
    else:
        try:
            plans = _load_raw(hd)
        except ValueError as exc:
            ok = False
            lines.append(f"FAIL  plans: unreadable ledger ({exc})")
        else:
            if not plans:
                ok = False
                lines.append("FAIL  plans: ledger is empty")
            else:
                unresolved = [f"{e.get('file')} ({e.get('status', 'pending')})"
                              for e in plans if e.get("status", "pending") != "done"]
                if unresolved:
                    ok = False
                    lines.append(f"FAIL  plans: unresolved — {', '.join(unresolved)}")
                else:
                    lines.append(f"ok    plans: {len(plans)}/{len(plans)} done")

    open_needs = []
    for e in plans:
        try:
            text = (hd / str(e.get("file"))).read_text()
        except OSError:
            continue  # missing plan doc: the ledger checks above stay authoritative
        match = _PROGRESS_RE.search(text)
        body = match.group(1) if match else ""
        if any(line.lstrip().startswith(_NEEDS_USER_PREFIX)
               for line in body.splitlines()):
            open_needs.append(str(e.get("file")))
    if open_needs:
        ok = False
        lines.append(f"FAIL  needs-user: open NEEDS-USER in {', '.join(open_needs)}")
    else:
        lines.append("ok    needs-user: none open")

    vpath = hd / "verify.json"
    try:
        verdicts = json.loads(vpath.read_text())
        if not (isinstance(verdicts, list) and verdicts):
            raise ValueError("not a non-empty JSON array")
    except OSError:
        ok = False
        lines.append("FAIL  verify: no verify.json — run casper_verify.py first")
    except ValueError as exc:
        ok = False
        lines.append(f"FAIL  verify: unusable verify.json ({exc})")
    else:
        failing = [v.get("criterion", "?") for v in verdicts if v.get("status") != "pass"]
        if failing:
            ok = False
            lines.append(f"FAIL  verify: not passing — {'; '.join(failing)}")
        else:
            lines.append(f"ok    verify: {len(verdicts)}/{len(verdicts)} criteria pass")

    lines.append("healthy" if ok else "NOT healthy")
    return (0 if ok else 1), lines


_KEEP = object()  # sentinel: "leave the recorded session fields untouched"


def set_status(handover_dir: Path, plan_file: str, status: str,
               session=_KEEP, pause_reason=_KEEP, zero_progress=_KEEP,
               session_backend=_KEEP) -> str:
    """Set a plan's status; on a pause, record the session and its provenance.

    `session_backend` is the CLI that minted `session` ("claude" | "pi" | None).
    It is additive and optional: omitting it preserves whatever is recorded, so
    callers that only pause a plan (e.g. the pre-dispatch NEEDS-USER path) never
    strip provenance from a still-resumable session.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    handover_dir = Path(handover_dir)
    with _locked(handover_dir):
        plans = _load_raw(handover_dir)
        e = _find(plans, plan_file)
        if e is None:
            return "not_found"
        e["status"] = status
        if status != "in_progress":
            e["lease"] = None
        if status == "paused":
            # Callers that pass values record/refresh them; callers that omit them
            # (e.g. a pre-dispatch NEEDS-USER pause) preserve the prior session so
            # a later answered re-dispatch can still warm-resume it.
            if session is not _KEEP:
                e["session"] = session
            if session_backend is not _KEEP:
                e["session_backend"] = session_backend
            if pause_reason is not _KEEP:
                e["pause_reason"] = pause_reason
            if zero_progress is not _KEEP:
                e["zero_progress"] = int(zero_progress)
        else:
            e["session"] = None  # any other transition invalidates the recorded session
            e["session_backend"] = None
            e["pause_reason"] = None
            e["zero_progress"] = 0
        e["updated_at"] = _now_iso()
        _save_raw(handover_dir, plans)
        stamp_frontmatter(handover_dir / e["file"], status)
        return status


# ----------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description="Atomic status ledger for casper plan docs.")
    ap.add_argument("--handover-dir", required=True, type=Path,
                    help="Per-goal handover dir containing plans.json")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true", help="Seed/merge ledger from a manifest")
    g.add_argument("--scaffold", action="store_true",
                   help="Create the default whole-goal plan doc + plans.json and seed "
                        "the ledger (idempotent; exit 1 without goal.md)")
    g.add_argument("--health", action="store_true",
                   help="Read-only completion audit: plans done, no open NEEDS-USER, "
                        "verify.json all-pass (exit 0 healthy / 1 not)")
    g.add_argument("--list-open", action="store_true", help="Print plan files needing work")
    g.add_argument("--claim", metavar="PLAN", help="Atomically take a plan -> in_progress")
    g.add_argument("--set", nargs=2, metavar=("PLAN", "STATUS"), dest="set_",
                   help=f"Set a plan's status ({'|'.join(STATUSES)})")
    ap.add_argument("--from-manifest", type=Path, default=None,
                    help="Manifest for --init (default: <handover-dir>/plans.json)")
    ap.add_argument("--stale-secs", type=int, default=DEFAULT_STALE_SECS,
                    help="in_progress older than this (s) is reclaimable")
    args = ap.parse_args()

    if args.init:
        try:
            n = init(args.handover_dir, args.from_manifest)
        except FileNotFoundError:
            src = args.from_manifest or ledger_path(args.handover_dir)
            print(f"no manifest at {src} — nothing to seed (run the planning step first)",
                  file=sys.stderr)
            return 2
        print(f"ledger seeded: {n} plan(s)")
        return 0
    if args.scaffold:
        try:
            res = scaffold(args.handover_dir)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{res['plan']}: {'created' if res['created_plan'] else 'kept as-is'}; "
              f"ledger seeded: {res['seeded']} plan(s)")
        if res["created_plan"]:
            print("fill its ## Objective before dispatch")
        return 0
    if args.health:
        code, lines = health(args.handover_dir)
        print("\n".join(lines))
        return code
    if args.list_open:
        for f in list_open(args.handover_dir, args.stale_secs):
            print(f)
        return 0
    if args.claim:
        res = claim(args.handover_dir, args.claim, args.stale_secs)
        print(res)
        return 3 if res == "not_found" else 0
    if args.set_:
        plan, status = args.set_
        if status not in STATUSES:
            ap.error(f"STATUS must be one of {'|'.join(STATUSES)}")
        res = set_status(args.handover_dir, plan, status)
        print(res)
        return 3 if res == "not_found" else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
