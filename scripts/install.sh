#!/usr/bin/env sh
# Install these workflows and agent instructions into your home directory.
#
#   ./scripts/install.sh              install
#   ./scripts/install.sh --dry-run    show what would happen, change nothing
#   ./scripts/install.sh --home DIR   install somewhere else (used by CI)
#
# What it does:
#   workflows/         -> $HOME/.agents/workflows/
#   guidelines/        -> $HOME/.agents/guidelines/
#   AGENTS.md          -> $HOME/.pi/agent/AGENTS.md
#                         $HOME/.codex/AGENTS.md
#                         $HOME/.opencode/AGENTS.md
#                         $HOME/.claude/CLAUDE.md
#   .env.example       -> $HOME/.agents/.env      (only if absent; chmod 600)
#   .config.example    -> $HOME/.agents/.config   (only if absent)
#
# Safe to re-run. Anything it would overwrite is backed up to <file>.bak.<stamp>
# first, and your filled-in .env / .config are never touched once they exist.
set -eu

REPO="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
DEST="${HOME:-}"
DRY=0
STAMP="$(date +%Y%m%d-%H%M%S)"

while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) DRY=1 ;;
	--home)
		shift
		[ $# -gt 0 ] || { echo "--home needs a directory" >&2; exit 2; }
		DEST="$1"
		;;
	-h | --help)
		sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "unknown option: $1" >&2
		exit 2
		;;
	esac
	shift
done

[ -n "$DEST" ] || { echo "HOME is not set; pass --home DIR" >&2; exit 2; }

say() { [ "$DRY" -eq 1 ] && echo "would $*" || echo "$*"; }

# Move an existing path aside instead of clobbering it.
backup() {
	[ -e "$1" ] || return 0
	say "back up $1 -> $1.bak.$STAMP"
	[ "$DRY" -eq 1 ] || mv "$1" "$1.bak.$STAMP"
}

install_file() {
	src="$1"
	dst="$2"
	if [ -e "$dst" ] && cmp -s "$src" "$dst"; then
		echo "unchanged $dst"
		return 0
	fi
	backup "$dst"
	say "install $dst"
	if [ "$DRY" -eq 0 ]; then
		mkdir -p "$(dirname "$dst")"
		cp "$src" "$dst"
	fi
}

# Copy only when absent: these hold the user's own secrets and settings.
seed_file() {
	src="$1"
	dst="$2"
	mode="$3"
	if [ -e "$dst" ]; then
		echo "kept $dst (already exists, not overwritten)"
		return 0
	fi
	say "create $dst from $(basename "$src")"
	if [ "$DRY" -eq 0 ]; then
		mkdir -p "$(dirname "$dst")"
		cp "$src" "$dst"
		chmod "$mode" "$dst"
	fi
}

echo "installing from $REPO into $DEST"
echo

# ── workflows ────────────────────────────────────────────────────────────────
WF_DEST="$DEST/.agents/workflows"
backup "$WF_DEST"
say "install $WF_DEST"
if [ "$DRY" -eq 0 ]; then
	mkdir -p "$DEST/.agents"
	cp -R "$REPO/workflows" "$WF_DEST"
	# Build artifacts are not part of the install.
	find "$WF_DEST" \( -name '__pycache__' -o -name '.pytest_cache' \) -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi

# ── guidelines ───────────────────────────────────────────────────────────────
GL_DEST="$DEST/.agents/guidelines"
backup "$GL_DEST"
say "install $GL_DEST"
if [ "$DRY" -eq 0 ]; then
	mkdir -p "$DEST/.agents"
	cp -R "$REPO/guidelines" "$GL_DEST"
	find "$GL_DEST" \( -name '__pycache__' -o -name '.pytest_cache' \) -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi

# ── agent instructions: one source file, four harness locations ──────────────
AGENTS="$REPO/AGENTS.md"
[ -f "$AGENTS" ] || { echo "missing $AGENTS" >&2; exit 1; }
install_file "$AGENTS" "$DEST/.pi/agent/AGENTS.md"
install_file "$AGENTS" "$DEST/.codex/AGENTS.md"
install_file "$AGENTS" "$DEST/.opencode/AGENTS.md"
install_file "$AGENTS" "$DEST/.claude/CLAUDE.md"

# ── secrets and settings ─────────────────────────────────────────────────────
seed_file "$REPO/.env.example" "$DEST/.agents/.env" 600
seed_file "$REPO/.config.example" "$DEST/.agents/.config" 644

echo
if [ "$DRY" -eq 1 ]; then
	echo "dry run: nothing was changed."
	exit 0
fi

cat <<EOF
Done.

Next:
  1. Fill in the credentials you need:  \$EDITOR $DEST/.agents/.env
  2. Adjust settings if you want:       \$EDITOR $DEST/.agents/.config
  3. Several workflows expect a Python env at $DEST/.agents/.venv:
       python3 -m venv $DEST/.agents/.venv

List the installed workflows and guidelines:
  $DEST/.agents/.venv/bin/python $WF_DEST/work-with-workflow/list_workflows.py
  $DEST/.agents/.venv/bin/python $GL_DEST/list_guidelines.py
EOF
