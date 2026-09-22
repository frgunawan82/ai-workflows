#!/usr/bin/env python3
"""Remove fully-resolved casper handover dirs — the final workflow step.

The handover dir is transient resumable state; once the goal is proven finished
it is noise. This script deletes it ONLY when that proof holds:
  - the dir looks like a casper handover (goal.md present; never / or $HOME)
  - the ledger exists and every plan is done (including no live in_progress lease)
  - verify.json exists and every criterion has status "pass"

Two modes, exactly one of which is required:

--handover-dir DIR   delete that one run dir (the workflow's own last step).
                     --force skips the resolved/verified checks (for abandoning
                     a goal); the shape check always applies. Refusal deletes
                     nothing, so it is always safe to attempt.

--sweep ROOT         reap leftovers in bulk: apply the same checks to every
                     immediate child DIRECTORY of ROOT (never recursing, never
                     touching ROOT itself, never following symlinked children).
                     This catches run dirs that were renamed (slug-archived,
                     slug-superseded) and so are invisible to the single-dir
                     step. --min-age-days N skips children touched within the
                     last N days; --dry-run reports without deleting.
                     HARD SAFETY: a child whose ledger shows a live (non-stale)
                     in_progress lease is NEVER deleted in sweep mode, and that
                     holds even under --force. --force otherwise reaps every
                     child that passes the shape check and the age filter,
                     whatever its verify state. Refusals are informational: the
                     sweep still exits 0.

Exit codes:
  0  removed (--handover-dir) / the sweep ran, refusals included (--sweep)
  1  refused — nothing deleted (--handover-dir only)
  2  usage error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import casper_status as cs  # bundled sibling; script dir is on sys.path

DAY_SECS = 86400


# ------------------------------------------------------------------- checks

def check_shape(hd: Path) -> str | None:
    """Refusal reason if `hd` is not a plausible casper handover dir, else None."""
    if hd in (Path("/"), Path.home()) or not (hd / "goal.md").exists():
        return f"{hd} does not look like a casper handover dir (no goal.md)"
    return None


def check_resolved_verified(hd: Path) -> tuple[str, str, list[str]] | None:
    """Refusal for a dir that is not fully resolved + verified, else None.

    Returns (short_reason, long_reason, detail_lines) — the long reason plus
    details are what the single-dir path has always printed to stderr; the
    short reason is the one-line form the sweep reports per child.
    """
    if not cs.ledger_path(hd).exists():
        return ("no ledger — nothing was dispatched",
                f"no ledger at {cs.ledger_path(hd)} — nothing was dispatched; "
                "use --force to remove an abandoned goal", [])
    open_plans = cs.list_unresolved(hd)
    if open_plans:
        return (f"unresolved plans: {', '.join(open_plans)}",
                "unresolved plans remain (re-run the fan-out, or --force):",
                [f"  {f}" for f in open_plans])
    vpath = hd / "verify.json"
    try:
        verdicts = json.loads(vpath.read_text())
        if not (isinstance(verdicts, list) and verdicts):
            raise ValueError("not a non-empty JSON array")
    except (OSError, ValueError) as e:
        return (f"verify.json unusable ({e})",
                f"{vpath} unusable ({e}) — run casper_verify.py first, or --force", [])
    failing = [v for v in verdicts if v.get("status") != "pass"]
    if failing:
        return ("criteria not passing: "
                + ", ".join(f"{v.get('criterion', '?')}={v.get('status')}" for v in failing),
                "criteria not passing:",
                [f"  {v.get('criterion', '?')}: {v.get('status')}" for v in failing])
    return None


def live_lease_reason(hd: Path, stale_secs: int = cs.DEFAULT_STALE_SECS) -> str | None:
    """Reason why `hd` may hold a live in_progress lease, else None.

    Staleness is decided by casper_status.is_open, so a lease that recorded a
    longer duration than our default is still honoured. An unreadable ledger
    cannot prove the absence of a live run, so it counts as live.
    """
    try:
        plans = cs._load_raw(hd)
    except (OSError, ValueError) as e:
        return f"unreadable ledger ({e}) — cannot prove no live lease"
    held = [str(e.get("file")) for e in plans
            if e.get("status") == "in_progress" and not cs.is_open(e, stale_secs)]
    if held:
        return f"live in_progress lease: {', '.join(held)}"
    return None


# --------------------------------------------------------------------- sweep

def _walk_stats(d: Path) -> tuple[float, int]:
    """(most recent mtime anywhere inside `d`, total bytes) without following symlinks."""
    newest = d.lstat().st_mtime
    total = 0
    for root, dirs, files in os.walk(d, followlinks=False):
        for name in dirs:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                continue
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
    return newest, total


def sweep(root: Path, force: bool, dry_run: bool, min_age_days: float,
          stale_secs: int = cs.DEFAULT_STALE_SECS) -> int:
    """Apply the single-dir checks to every immediate child dir of `root`."""
    counts = {"removed": 0, "would-remove": 0, "refused": 0,
              "skipped-too-new": 0, "skipped-live-lease": 0}
    reclaimed = 0
    min_age_secs = min_age_days * DAY_SECS
    now = time.time()

    try:
        children = sorted(e.path for e in os.scandir(root)
                          if e.is_dir(follow_symlinks=False))
    except OSError as e:
        print(f"usage error: cannot read {root} ({e})", file=sys.stderr)
        return 2

    def report(action: str, child: Path, reason: str) -> None:
        counts[action] += 1
        print(f"{action:<16} {child}  — {reason}")

    for path in children:
        child = Path(path)
        shape = check_shape(child)
        if shape:
            report("refused", child, shape)
            continue
        newest, nbytes = _walk_stats(child)
        age_secs = now - newest
        if age_secs < min_age_secs:
            report("skipped-too-new", child,
                   f"touched {age_secs / DAY_SECS:.2f}d ago < --min-age-days {min_age_days:g}")
            continue
        lease = live_lease_reason(child, stale_secs)
        if lease:  # hard safety: holds even under --force
            report("skipped-live-lease", child, lease)
            continue
        if not force:
            problem = check_resolved_verified(child)
            if problem:
                report("refused", child, problem[0])
                continue
        reason = "forced (shape ok, no live lease)" if force else "resolved + verified"
        if dry_run:
            report("would-remove", child, f"{reason}, {nbytes} bytes")
            continue
        try:
            shutil.rmtree(child)
        except OSError as e:
            report("refused", child, f"rmtree failed ({e})")
            continue
        reclaimed += nbytes
        report("removed", child, f"{reason}, {nbytes} bytes")

    print(f"sweep {root}: {counts['removed']} removed, "
          f"{counts['would-remove']} would-remove, {counts['refused']} refused, "
          f"{counts['skipped-too-new']} skipped-too-new, "
          f"{counts['skipped-live-lease']} skipped-live-lease; "
          f"{reclaimed} bytes reclaimed")
    return 0


# ----------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Remove fully-resolved casper handover dirs (one, or a sweep).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--handover-dir", type=Path, help="Delete this one run dir")
    g.add_argument("--sweep", type=Path, metavar="ROOT",
                   help="Reap eligible immediate child dirs of ROOT (no recursion)")
    ap.add_argument("--force", action="store_true",
                    help="Skip the resolved/verified checks (abandoned goal); the shape "
                         "check always applies, and a sweep still never deletes a child "
                         "holding a live in_progress lease")
    ap.add_argument("--min-age-days", type=float, default=0, metavar="N",
                    help="Sweep only: skip a child touched within the last N days "
                         "(default 0 = no age filter)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Sweep only: report what would be removed, delete nothing")
    args = ap.parse_args()

    if args.handover_dir is not None:
        if args.dry_run:
            ap.error("--dry-run applies to --sweep only")
        if args.min_age_days:
            ap.error("--min-age-days applies to --sweep only")
        return clean_one(args.handover_dir, args.force)

    root = args.sweep.resolve()
    if not root.is_dir():
        print(f"usage error: --sweep {root} is not a directory", file=sys.stderr)
        return 2
    return sweep(root, args.force, args.dry_run, args.min_age_days)


def clean_one(handover_dir: Path, force: bool) -> int:
    hd = handover_dir.resolve()
    shape = check_shape(hd)
    if shape:
        print(f"refusing: {shape}", file=sys.stderr)
        return 1

    if not force:
        problem = check_resolved_verified(hd)
        if problem:
            print(f"refusing: {problem[1]}", file=sys.stderr)
            for line in problem[2]:
                print(line, file=sys.stderr)
            return 1

    shutil.rmtree(hd)
    print(f"removed {hd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
