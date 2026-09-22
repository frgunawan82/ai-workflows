#!/usr/bin/env bash
# merge-when-green.sh — wait for every PR check to finish, then merge ONLY if
# every one of them is green. Repo-agnostic: run it from anywhere, against any
# repository, with no assumption about where this script itself lives.
#
# WHY THIS EXISTS. Use it wherever branch protection or required status checks
# are unavailable or not configured. `gh pr merge --auto` waits on *branch-
# protection rules*; with none configured it does not queue — it merges
# IMMEDIATELY, whatever the checks are doing. This script is the missing wait.
#
# WHAT THIS IS NOT. A convention, strictly weaker than branch protection. It
# gates only merges that go THROUGH it: a human or agent typing `gh pr merge`
# directly still bypasses it completely, because nothing server-side can refuse
# them. Real branch protection / rulesets are the only real fix.
#
# NEVER BYPASSES ANYTHING. There is no `--admin`, no `--force`, no "merge
# anyway": those flags are refused at argument parsing so a caller cannot talk
# this script into the exact behaviour it exists to prevent. Red checks are a
# hard refusal with a non-zero exit.
#
# Check-state handling (classified by `gh pr checks --json`'s own `bucket`
# field, which is gh's documented normalisation of the raw `state`):
#   pass      SUCCESS                       -> green
#   skipping  SKIPPED / NEUTRAL             -> green by default (a path-filtered
#                                             or Dependabot-only required job
#                                             legitimately skips on most PRs).
#                                             Pass --strict-skipped to call it
#                                             RED where a skipped required job
#                                             means it never ran.
#   pending   QUEUED / IN_PROGRESS / …      -> not terminal; keep polling
#   fail      FAILURE / TIMED_OUT / …       -> RED, refuse
#   cancel    CANCELLED                     -> RED, refuse. A cancelled run
#                                             asserted nothing, and cancellation
#                                             is routine, not a freak race:
#                                             `cancel-in-progress: true` means a
#                                             newer push DELIBERATELY supersedes
#                                             an in-flight run. A policy-
#                                             cancelled run must never read as a
#                                             pass.
#   <unknown> a bucket a newer gh invents   -> RED, refuse (fail closed: an
#                                             unrecognised state is never a pass)
#   no checks reported at all               -> NEVER green. `gh pr checks` returns
#                                             [] for the first seconds after a
#                                             push, and reading that as success is
#                                             the whole hole this closes. Checks
#                                             must appear within --appear-timeout
#                                             or this exits 5.
#
# A red check is reported the moment it is seen, without waiting for the still-
# running checks to finish: the outcome cannot change (this script will not merge
# either way) and the developer gets the failure minutes earlier. Both the red and
# the still-pending checks are named in that report.
#
# Exit codes:
#   0  merged — or already merged, so a re-run is a safe no-op (idempotent)
#   2  usage error, a missing dependency, no git repo, or a refused bypass flag
#   3  at least one check is not green -> refused to merge
#   4  timed out (--timeout) with checks still not terminal
#   5  no checks reported within --appear-timeout
#   6  the merge command failed, or the PR is somehow not MERGED afterwards
#   7  the PR is closed without having been merged
set -euo pipefail

# Poll budget. Env vars override the defaults; explicit flags override the env.
TIMEOUT="${MWG_TIMEOUT:-2700}"
INTERVAL="${MWG_INTERVAL:-20}"
APPEAR_TIMEOUT="${MWG_APPEAR_TIMEOUT:-300}"

usage() {
  local me
  me="$(basename "$0")"
  cat <<EOF
Wait for every PR check to finish, then merge only if all of them are green.

Usage:
  $me <pr-number> [--repo <owner>/<name>] [--timeout <sec>] [--interval <sec>]
                  [--appear-timeout <sec>] [--merge-method squash|merge|rebase]
                  [--no-delete-branch] [--strict-skipped]
  $me --evaluate <file|->   classify a checks JSON payload
  $me -h | --help

Options:
  --repo <owner>/<name>  operate on that repository; no local checkout needed.
                         Without it, the repo is the one containing \$PWD.
  --merge-method <m>     squash (default) | merge | rebase
  --no-delete-branch     keep the head branch (default: delete it)
  --strict-skipped       treat the 'skipping' bucket as RED, not green
  --timeout <sec>        total poll budget          (env MWG_TIMEOUT, default 2700)
  --interval <sec>       seconds between polls      (env MWG_INTERVAL, default 20)
  --appear-timeout <sec> how long checks may take
                         to show up at all          (env MWG_APPEAR_TIMEOUT, default 300)

Exit: 0 merged/already-merged, 2 usage, 3 not green, 4 poll timeout,
      5 no checks reported, 6 merge failed, 7 PR closed unmerged.
EOF
}

log() {
  printf '%s  %s\n' "$(date -u '+%H:%M:%SZ')" "$*"
}

die() {
  local code="$1"
  shift
  printf 'merge-when-green: %s\n' "$*" >&2
  exit "$code"
}

# The whole state-evaluation contract, in one place, so the polling loop below
# and the `--evaluate` mode the tests drive are literally the same code path.
# Prints a human-readable report; the verdict is the exit code:
#   0 green | 3 red | 4 pending | 5 none reported
# argv[1] is a payload FILE, never stdin, so it cannot collide with a heredoc.
# argv[2] is "strict" or "lenient" for the `skipping` bucket.
PY_EVALUATE="$(
  cat <<'PY'
import json
import sys

# gh documents bucket as its normalisation of `state` into exactly these five
# values. Anything outside the green ones is refused, so a sixth bucket
# introduced by a future gh is red rather than silently mergeable.
strict_skipped = len(sys.argv) > 2 and sys.argv[2] == "strict"
GREEN = {"pass"} if strict_skipped else {"pass", "skipping"}
RED = {"fail", "cancel", "skipping"} if strict_skipped else {"fail", "cancel"}
PENDING = {"pending"}

with open(sys.argv[1], encoding="utf-8") as handle:
    text = handle.read()

try:
    checks = json.loads(text) if text.strip() else []
except json.JSONDecodeError as exc:
    print(f"unparseable `gh pr checks --json` payload: {exc}")
    sys.exit(3)

if not isinstance(checks, list):
    print(f"expected a JSON array of checks, got {type(checks).__name__}")
    sys.exit(3)


def describe(check):
    name = str(check.get("name") or "<unnamed>")
    bucket = str(check.get("bucket") or "<empty>")
    state = str(check.get("state") or "<empty>")
    link = str(check.get("link") or "")
    tail = f"  {link}" if link else ""
    return f"    - {name} [bucket={bucket} state={state}]{tail}"


green, red, pending = [], [], []
for check in checks:
    if not isinstance(check, dict):
        red.append({"name": repr(check), "bucket": "<malformed>", "state": "<malformed>"})
        continue
    bucket = str(check.get("bucket") or "").lower()
    if bucket in GREEN:
        green.append(check)
    elif bucket in RED:
        red.append(check)
    elif bucket in PENDING:
        pending.append(check)
    else:
        # Fail closed. An unrecognised bucket is the one case where guessing
        # "probably fine" would merge something nobody verified.
        red.append(check)

if not checks:
    print("no checks reported for this head — refusing to read that as success")
    sys.exit(5)

if red:
    print(f"{len(red)} check(s) NOT green:")
    for check in red:
        print(describe(check))
    if pending:
        print(f"{len(pending)} check(s) still running (outcome cannot change):")
        for check in pending:
            print(describe(check))
    sys.exit(3)

if pending:
    print(f"waiting on {len(pending)} of {len(checks)} check(s):")
    for check in pending:
        print(describe(check))
    sys.exit(4)

skipped = sum(1 for check in green if str(check.get("bucket") or "").lower() == "skipping")
print(f"all {len(checks)} check(s) terminal and green ({len(green) - skipped} passed, {skipped} skipped):")
for check in green:
    print(describe(check))
sys.exit(0)
PY
)"

STRICT_SKIPPED=lenient

evaluate_checks() {
  python3 -c "$PY_EVALUATE" "$1" "$STRICT_SKIPPED"
}

PR=""
EVALUATE_PATH=""
REPO=""
MERGE_METHOD=squash
DELETE_BRANCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --evaluate)
      [ $# -ge 2 ] || die 2 "--evaluate needs a file path or '-'"
      EVALUATE_PATH="$2"
      shift 2
      ;;
    --repo)
      [ $# -ge 2 ] || die 2 "--repo needs <owner>/<name>"
      REPO="$2"
      shift 2
      ;;
    --merge-method)
      [ $# -ge 2 ] || die 2 "--merge-method needs squash|merge|rebase"
      case "$2" in
        squash | merge | rebase) MERGE_METHOD="$2" ;;
        *) die 2 "unknown --merge-method '$2' — expected squash, merge, or rebase" ;;
      esac
      shift 2
      ;;
    --no-delete-branch)
      DELETE_BRANCH=0
      shift
      ;;
    --strict-skipped)
      STRICT_SKIPPED=strict
      shift
      ;;
    --timeout)
      [ $# -ge 2 ] || die 2 "--timeout needs seconds"
      TIMEOUT="$2"
      shift 2
      ;;
    --interval)
      [ $# -ge 2 ] || die 2 "--interval needs seconds"
      INTERVAL="$2"
      shift 2
      ;;
    --appear-timeout)
      [ $# -ge 2 ] || die 2 "--appear-timeout needs seconds"
      APPEAR_TIMEOUT="$2"
      shift 2
      ;;
    # Refused on purpose. Each of these is a way to land a PR whose checks were
    # never satisfied, which is the single thing this script exists to stop.
    --admin | --force | -f | --no-verify | --skip-checks | --ignore-checks | --disable-checks)
      die 2 "refusing '$1' — this script never bypasses a check. Fix the failure instead."
      ;;
    -*)
      die 2 "unknown flag: $1"
      ;;
    *)
      [ -z "$PR" ] || die 2 "unexpected extra argument: $1"
      PR="$1"
      shift
      ;;
  esac
done

if [ -n "$EVALUATE_PATH" ]; then
  [ -z "$PR" ] || die 2 "--evaluate takes no PR number"
  command -v python3 >/dev/null 2>&1 || die 2 "python3 is not on PATH"
  payload="$(mktemp)"
  # shellcheck disable=SC2064  # expand $payload now, at trap-install time
  trap "rm -f '$payload'" EXIT
  if [ "$EVALUATE_PATH" = "-" ]; then
    cat >"$payload"
  else
    [ -f "$EVALUATE_PATH" ] || die 2 "no such payload file: $EVALUATE_PATH"
    cat "$EVALUATE_PATH" >"$payload"
  fi
  set +e
  evaluate_checks "$payload"
  verdict=$?
  set -e
  exit "$verdict"
fi

[ -n "$PR" ] || {
  usage >&2
  exit 2
}
case "$PR" in
  *[!0-9]* | "") die 2 "PR must be a number, got '$PR'" ;;
esac
for budget in "$TIMEOUT" "$INTERVAL" "$APPEAR_TIMEOUT"; do
  case "$budget" in
    *[!0-9]* | "") die 2 "timeout/interval values must be whole seconds, got '$budget'" ;;
  esac
done

command -v gh >/dev/null 2>&1 || die 2 "gh is not on PATH"
command -v python3 >/dev/null 2>&1 || die 2 "python3 is not on PATH"

# Repo selection. `--repo <owner>/<name>` is passed through to every gh call and
# needs no checkout at all; otherwise the repository is the one containing the
# CALLER's cwd — never a path derived from where this script happens to live.
GH_REPO_ARGS=()
REPO_ROOT=""
if [ -n "$REPO" ]; then
  case "$REPO" in
    */*) ;;
    *) die 2 "--repo must be <owner>/<name>, got '$REPO'" ;;
  esac
  GH_REPO_ARGS=(--repo "$REPO")
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$REPO_ROOT" ] || die 2 "not inside a git repository (cwd: $PWD) — run this from a checkout, or pass --repo <owner>/<name>."
fi

gh_pr() {
  gh pr "$@" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}"
}

pr_state() {
  gh_pr view "$PR" --json state --jq '.state' 2>/dev/null || true
}

# Idempotent by design: re-running after a successful merge (or after a crash
# between merge and cleanup) must report and succeed, never error.
state="$(pr_state)"
case "$state" in
  MERGED)
    log "PR #$PR is already MERGED — nothing to do."
    exit 0
    ;;
  CLOSED)
    die 7 "PR #$PR is CLOSED without being merged; reopen it before merging."
    ;;
  OPEN) ;;
  *)
    die 2 "cannot read the state of PR #$PR (got '${state:-<none>}'); is the number right?"
    ;;
esac

log "PR #$PR: waiting for every check to finish (timeout ${TIMEOUT}s, poll ${INTERVAL}s)."
log "Checks must appear within ${APPEAR_TIMEOUT}s; no checks is never a pass."

payload="$(mktemp)"
# shellcheck disable=SC2064  # expand $payload now, at trap-install time
trap "rm -f '$payload' '$payload.err'" EXIT

elapsed=0
while :; do
  if ! gh_pr checks "$PR" --json name,state,bucket,link >"$payload" 2>"$payload.err"; then
    # `gh pr checks` exits 1 on failure and 8 while pending, so a non-zero exit
    # is expected and says nothing on its own — the JSON body is the signal. Only
    # an empty body means the call itself did not produce a payload.
    if [ ! -s "$payload" ]; then
      log "gh pr checks produced no output: $(tr '\n' ' ' <"$payload.err")"
      printf '[]' >"$payload"
    fi
  fi
  rm -f "$payload.err"

  set +e
  report="$(evaluate_checks "$payload" 2>&1)"
  verdict=$?
  set -e
  printf '%s\n' "$report"

  case "$verdict" in
    0)
      log "PR #$PR: all checks green."
      break
      ;;
    3)
      die 3 "PR #$PR has checks that are not green — refusing to merge. Fix them, push, and re-run."
      ;;
    4)
      if [ "$elapsed" -ge "$TIMEOUT" ]; then
        die 4 "PR #$PR: timed out after ${elapsed}s with checks still running. Re-run once they finish, or raise --timeout."
      fi
      ;;
    5)
      if [ "$elapsed" -ge "$APPEAR_TIMEOUT" ]; then
        die 5 "PR #$PR: no checks reported after ${elapsed}s. Zero checks is NOT a pass — confirm the PR workflows are triggering (gh pr checks $PR; gh run list) before merging anything."
      fi
      log "PR #$PR: no checks yet (${elapsed}s of ${APPEAR_TIMEOUT}s allowed) — still waiting for them to appear."
      ;;
    *)
      die 2 "unexpected evaluation exit $verdict"
      ;;
  esac

  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
done

MERGE_ARGS=("--$MERGE_METHOD")
if [ "$DELETE_BRANCH" -eq 1 ]; then
  MERGE_ARGS+=(--delete-branch)
  log "PR #$PR: ${MERGE_METHOD}-merging (delete-branch)."
else
  log "PR #$PR: ${MERGE_METHOD}-merging (keeping the head branch)."
fi

# `gh pr merge --delete-branch` run from inside a LINKED WORKTREE can leave the
# local repo with core.bare=true as a side effect of its branch cleanup. Capture
# the setting first and restore it only if it was genuinely false beforehand —
# blindly forcing false would corrupt an intentionally bare repository. Skipped
# entirely in --repo mode, where there may be no local checkout to protect.
bare_before=""
if [ -n "$REPO_ROOT" ]; then
  bare_before="$(git -C "$REPO_ROOT" config core.bare 2>/dev/null || true)"
fi

merge_status=0
gh_pr merge "$PR" "${MERGE_ARGS[@]}" || merge_status=$?

if [ "$bare_before" = "false" ]; then
  bare_after="$(git -C "$REPO_ROOT" config core.bare 2>/dev/null || true)"
  if [ "$bare_after" != "false" ]; then
    git -C "$REPO_ROOT" config core.bare false
    echo "WARN: core.bare was false before the merge and is now '${bare_after:-<unset>}' — restored to false." >&2
  fi
fi

# The PR's own state is the authority on whether the merge happened, NOT gh's
# exit code. Run from inside a linked worktree, `--delete-branch` fails its LOCAL
# branch delete ("fatal: 'main' is already used by worktree at …") and gh exits
# non-zero long after the remote merge succeeded. Reading that as a failed merge
# is how a caller talks itself into re-merging, or into believing an unchecked
# merge errored when in truth the PR had already landed.
#
# Re-read a few times: the merge is committed but GitHub's read path can lag a
# second behind it, and reporting a landed merge as a failure is its own hazard.
state=""
for _ in 1 2 3 4 5; do
  state="$(pr_state)"
  [ "$state" = "MERGED" ] && break
  sleep 1
done
if [ "$state" = "MERGED" ]; then
  if [ "$merge_status" -ne 0 ]; then
    log "PR #$PR: MERGED — but gh exited $merge_status, almost always the LOCAL"
    log "branch delete failing from inside a linked worktree. The merge itself is done;"
    log "delete the local branch during your normal cleanup."
  else
    log "PR #$PR: MERGED."
  fi
  exit 0
fi
die 6 "PR #$PR: 'gh pr merge ${MERGE_ARGS[*]}' exited $merge_status and the PR is '${state:-<none>}', not MERGED. Nothing was bypassed; resolve the cause and re-run."
