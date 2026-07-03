#!/usr/bin/env bash
# scripts/blast-radius.sh — blast-radius regression-safety gate.
#
# Identifies which test files exercise changed source/script/hook files,
# then runs ONLY those tests.  When NO source files changed (only docs/meta),
# skips the test gate entirely with an early green.
#
# Usage:
#   bash scripts/blast-radius.sh                        # compare HEAD vs origin/main
#   bash scripts/blast-radius.sh --base some-branch     # compare against another base
#   bash scripts/blast-radius.sh --all-tests            # ignore blast-radius, run full suite
#
# Exit codes:
#   0 — gate passed (all targeted tests green, or nothing to test)
#   1 — one or more targeted tests failed
#   2 — internal error (bad args, missing tools, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE="${1:-origin/main}"
BASE_BRANCH="$BASE"
RUN_ALL=false
PYTHON="${PYTHON:-python3}"

# ---- arg parsing ----

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)    BASE_BRANCH="$2"; shift 2 ;;
    --all-tests) RUN_ALL=true; shift ;;
    --help|-h)
      echo "Usage: bash $0 [--base <branch>] [--all-tests]"
      echo ""
      echo "  --base <branch>   Compare against <branch> instead of origin/main"
      echo "  --all-tests       Run full test suite instead of blast-radius subset"
      exit 0
      ;;
    *)
      # positional: treat as base branch
      BASE_BRANCH="$1"
      shift
      ;;
  esac
done

cd "$REPO_DIR"

# ---- helpers ----

_hr() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  $*"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

_red()  { printf "\033[31m%s\033[0m\n" "$*"; }
_green(){ printf "\033[32m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }

# ---- find changed files ----

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  _red "❌ Not inside a git repository"
  exit 2
fi

# Resolve the base ref (might be origin/main or a local branch).
# Fall back to HEAD~1 if the base ref doesn't exist (fresh branch with no remote).
if git rev-parse --verify "$BASE_BRANCH" >/dev/null 2>&1; then
  BASE_REF="$BASE_BRANCH"
elif git rev-parse --verify "origin/$BASE_BRANCH" >/dev/null 2>&1; then
  BASE_REF="origin/$BASE_BRANCH"
else
  _yellow "⚠ Base ref '$BASE_BRANCH' not found — comparing against HEAD~1"
  BASE_REF="HEAD~1"
fi

_hr "🔍 Blast-radius: comparing changed files against $(git rev-parse --short "$BASE_REF")"

CHANGED_FILES=$(git diff --name-only "$BASE_REF"...HEAD 2>/dev/null || git diff --name-only "$BASE_REF"..HEAD 2>/dev/null || true)

if [ -z "$CHANGED_FILES" ]; then
  _green "✔ No changed files detected — nothing to test"
  exit 0
fi

echo "Changed files:"
echo "$CHANGED_FILES" | sed 's/^/  • /'

# ---- classify changed files ----

TESTS_DIR="$REPO_DIR/tests"

# Build sets of changed files by category.
CHANGED_TESTS=()
CHANGED_SOURCE=()
CHANGED_SCRIPTS=()
CHANGED_HOOKS=()
CHANGED_OTHER=()

while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    tests/test_*.py)          CHANGED_TESTS+=("$f") ;;
    tests/*.py)               CHANGED_TESTS+=("$f") ;;
    simplicio_loop/*.py)      CHANGED_SOURCE+=("$f") ;;
    simplicio_loop/**/*.py)   CHANGED_SOURCE+=("$f") ;;
    scripts/*.py)             CHANGED_SCRIPTS+=("$f") ;;
    scripts/*.sh)             CHANGED_SCRIPTS+=("$f") ;;
    hooks/*.py)               CHANGED_HOOKS+=("$f") ;;
    hooks/*.sh)               CHANGED_HOOKS+=("$f") ;;
    *)                        CHANGED_OTHER+=("$f") ;;
  esac
done <<< "$CHANGED_FILES"

echo ""
echo "  Tests changed:   ${#CHANGED_TESTS[@]}"
echo "  Source changed:  ${#CHANGED_SOURCE[@]}"
echo "  Scripts changed: ${#CHANGED_SCRIPTS[@]}"
echo "  Hooks changed:   ${#CHANGED_HOOKS[@]}"
echo "  Other changed:   ${#CHANGED_OTHER[@]}"

# ---- blast-radius: resolve which tests to run ----

TARGET_TESTS=()

# 1. Explicitly changed test files — always run.
for tf in "${CHANGED_TESTS[@]}"; do
  TARGET_TESTS+=("$REPO_DIR/$tf")
done

# 2. For each changed source/script/hook file, find tests that import it.
resolve_affected_tests() {
  local changed_path="$1"
  local basename_val
  basename_val="$(basename "$changed_path" .py)"

  # Strip directory prefix(es) to get the Python module name.
  local modname
  modname="$(echo "$changed_path" \
    | sed -E 's|^simplicio_loop/||' \
    | sed -E 's|^scripts/||' \
    | sed -E 's|^hooks/||' \
    | sed -E 's|\.py$||' \
    | tr '/' '.')"

  # Determine which subdirectory the file is in (scripts/, hooks/, simplicio_loop/).
  local subdir=""
  case "$changed_path" in
    scripts/*)        subdir="scripts" ;;
    hooks/*)          subdir="hooks" ;;
    simplicio_loop/*) subdir="simplicio_loop" ;;
  esac

  # Search test files for:
  #   (a) Python imports: "import modname", "from modname import", "from simplicio_loop.modname"
  #   (b) Path references: os.path.join(REPO, "subdir", "filename.py")
  #   (c) Path references: os.path.join(HOOKS_DIR, "filename.py")  etc.
  while IFS= read -r -d '' testfile; do
    if grep -qE "(import ${modname}[^a-zA-Z_]|from ${modname}[[:space:]]+import|from simplicio_loop\\.${modname}[^a-zA-Z_])" "$testfile" 2>/dev/null; then
      echo "$testfile"
    elif [ -n "$subdir" ] && grep -qE "\"${subdir}\"[[:space:]]*,[[:space:]]*\"${basename_val}\"" "$testfile" 2>/dev/null; then
      echo "$testfile"
    fi
  done < <(find "$TESTS_DIR" -name 'test_*.py' -print0 2>/dev/null)
}

if [ "$RUN_ALL" = true ]; then
  _yellow "⚠ --all-tests: running full suite (bypassing blast-radius)"
  # Run all tests via check.py
  exec "$PYTHON" "$SCRIPT_DIR/check.py" --tests-only
fi

# If only docs/config/meta files changed (no code), skip tests — early green.
if [ ${#CHANGED_TESTS[@]} -eq 0 ] \
   && [ ${#CHANGED_SOURCE[@]} -eq 0 ] \
   && [ ${#CHANGED_SCRIPTS[@]} -eq 0 ] \
   && [ ${#CHANGED_HOOKS[@]} -eq 0 ]; then
  _green "✔ Only non-code files changed — skipping test gate"
  exit 0
fi

# Resolve affected tests for changed source, scripts, and hooks.
for path in "${CHANGED_SOURCE[@]}" "${CHANGED_SCRIPTS[@]}" "${CHANGED_HOOKS[@]}"; do
  while IFS= read -r tf; do
    [ -n "$tf" ] && TARGET_TESTS+=("$tf")
  done < <(resolve_affected_tests "$path" || true)
done

# Also find tests that depend on hooks via their _bundle path.
for path in "${CHANGED_HOOKS[@]}"; do
  hook_basename="$(basename "$path" .py)"
  while IFS= read -r -d '' testfile; do
    if grep -qE "simplicio_loop\._bundle\.hooks\.${hook_basename}" "$testfile" 2>/dev/null; then
      TARGET_TESTS+=("$testfile")
    fi
  done < <(find "$TESTS_DIR" -name 'test_*.py' -print0 2>/dev/null)
done

# Deduplicate and sort.
TARGET_TESTS=($(printf "%s\n" "${TARGET_TESTS[@]}" | sort -u))

# ---- run the gate ----

if [ ${#TARGET_TESTS[@]} -eq 0 ]; then
  _green "✔ No tests appear to exercise the changed files — blast-radius gate skipped"
  exit 0
fi

_hr "🧪 Blast-radius: running ${#TARGET_TESTS[@]} affected test(s)"
for tf in "${TARGET_TESTS[@]}"; do
  echo "  • $(basename "$tf")"
done
echo ""

# Check if pytest is available.
HAS_PYTEST=false
if "$PYTHON" -c "import pytest" 2>/dev/null; then
  HAS_PYTEST=true
fi

FAILED=0
for tf in "${TARGET_TESTS[@]}"; do
  test_name="$(basename "$tf")"
  echo "── Running $test_name ──"
  set +e
  if [ "$HAS_PYTEST" = true ]; then
    "$PYTHON" -m pytest -q "$tf" 2>&1
  else
    "$PYTHON" "$tf" 2>&1
  fi
  rc=$?
  set -e
  if [ $rc -eq 0 ]; then
    _green "  ✔ $test_name PASSED"
  else
    _red "  ✘ $test_name FAILED (exit $rc)"
    FAILED=$((FAILED + 1))
  fi
  echo ""
done

# ---- summary ----

_hr "📊 Blast-radius gate summary"
echo "  Total affected tests: ${#TARGET_TESTS[@]}"
echo "  Passed:              $(( ${#TARGET_TESTS[@]} - FAILED ))"
echo "  Failed:              $FAILED"
echo ""

if [ $FAILED -eq 0 ]; then
  _green "✔ Blast-radius gate PASSED"
  exit 0
else
  _red "✘ Blast-radius gate FAILED — $FAILED test(s) regressed"
  exit 1
fi

