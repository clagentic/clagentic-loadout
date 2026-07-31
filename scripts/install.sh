#!/usr/bin/env sh
# install.sh — parameterized installer for clagentic-loadout.
#
# Installs the clagentic_loadout package (from a local checkout, an sdist/
# wheel, or PyPI once published) and makes its console_scripts
# (clagentic-loadout, loadout-poll-wait, loadout-scoped-test-wait,
# loadout-release-dispatch, loadout-release-detect, loadout-git-host-api,
# loadout-review-post, loadout-push, loadout-merge,
# loadout-provision-allowlist) resolvable on PATH for
# whatever environment invokes it next -- interactively, or an agent-spawn
# environment that starts a fresh, non-interactive shell per invocation.
#
# This is the generalization of an earlier one-off /usr/local/bin symlink
# fix: instead of a single hand-maintained symlink for one binary in
# one deployment, this script (a) picks the best available installer
# (pipx > uv > pip --user > venv, in that preference order -- see
# _detect_installer below for why), (b) installs the package through it, and
# (c) verifies + repairs PATH visibility for the resulting console_scripts
# directory, so a subsequent PATH-prepend workaround step in a calling
# harness's own spawn script becomes unnecessary.
#
# The venv tier exists for PEP 668 "externally-managed-environment"
# systems (stock Debian/Ubuntu Python) where neither pipx nor uv is present:
# a bare `pip install --user` is refused there, so this script falls through
# to a self-managed venv under the resolved data dir, then symlinks its
# console_scripts out into a PATH directory. This NEVER passes
# --break-system-packages -- that flag defeats the protection PEP 668 exists
# to provide and is not offered as a default anywhere in this script.
#
# No operator hostnames, no absolute /workspace paths, no lore references —
# runnable by anyone against any checkout or published distribution.
#
# Usage:
#   scripts/install.sh                     # install this checkout (editable
#                                           # if PIP_EDITABLE=1, else a normal
#                                           # build) via the best available
#                                           # installer
#   scripts/install.sh --source <path>     # install from a different sdist/
#                                           # wheel/checkout path
#   scripts/install.sh --installer pipx    # force a specific installer
#                                           # (pipx|uv|pip|venv)
#   scripts/install.sh --editable          # editable/dev install (pip/venv
#                                           # only; pipx/uv passthrough via
#                                           # their own --editable flag where
#                                           # supported)
#   scripts/install.sh --path-dir <dir>    # candidate console_scripts bin dir
#                                           # to verify/append to PATH,
#                                           # instead of each installer's own
#                                           # default (repeatable)
#   scripts/install.sh --data-dir <dir>    # base dir for the self-managed
#                                           # venv tier (default:
#                                           # ~/.local/share/clagentic/loadout,
#                                           # override via
#                                           # CLAGENTIC_LOADOUT_HOME)
#   scripts/install.sh --bin-dir <dir>     # symlink-target bin dir for the
#                                           # venv tier's console_scripts
#                                           # (default: ~/.local/bin, override
#                                           # via CLAGENTIC_LOADOUT_BIN_DIR)
#   scripts/install.sh --git-host-base-url <url>
#                                           # seed ~/.config/clagentic/loadout/
#                                           # config.yaml's git_host.base_url
#                                           # key (the value
#                                           # transport.git_host_api's
#                                           # _resolve_git_host_base config-
#                                           # file tier reads) so no
#                                           # deployment has to hand-export a
#                                           # base-URL env var. See
#                                           # "Git-host config seeding" below.
#   scripts/install.sh --skills-dir <dir>  # target dir for the global
#                                           # .claude/skills/-style skill
#                                           # install (default:
#                                           # ~/.claude/skills, override via
#                                           # CLAGENTIC_LOADOUT_SKILLS_DIR).
#                                           # See "Skill install" below.
#   scripts/install.sh --dry-run           # print the resolved plan, install
#                                           # nothing
#   scripts/install.sh --help
#
# Exit codes:
#   0   installed (or --dry-run printed the plan) successfully
#   1   usage error (bad flag, unreadable --source path, HOME empty/unset
#       with no compensating override)
#   2   no supported installer found on this system (no pipx, uv, pip3/pip,
#       or a working python3 -m venv for the terminal fallback)
#   3   the underlying installer command failed
#
# Environment overrides (all optional):
#   CLAGENTIC_LOADOUT_INSTALLER       same as --installer
#   CLAGENTIC_LOADOUT_SOURCE          same as --source
#   CLAGENTIC_LOADOUT_HOME            same as --data-dir (venv tier base dir)
#   CLAGENTIC_LOADOUT_BIN_DIR         same as --bin-dir (venv tier
#                                     symlink-target bin dir)
#   CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL
#                                     same as --git-host-base-url
#   CLAGENTIC_LOADOUT_SKILLS_DIR      same as --skills-dir
#   PIP_EDITABLE=1                    same as --editable, for the pip/venv
#                                     fallback paths
#
# Skill install:
#   This installer also copies this repo's global, LLM-invocable skills
#   (currently: loadout-init, .claude/skills/loadout-init/) out to a
#   HOME-derived skills directory -- default ~/.claude/skills/<skill-name>/,
#   mirroring the same "console_scripts land on PATH" guarantee this script
#   already makes for the Python entry points, applied to skill discovery
#   instead of executable discovery. --skills-dir/CLAGENTIC_LOADOUT_SKILLS_DIR
#   overrides the target dir (e.g. a project-level .claude/skills/ instead of
#   the global one). Idempotent: re-running overwrites only files this
#   installer itself owns (a straight directory copy per skill, mirroring the
#   venv tier's own "re-running installs into the SAME target" contract) --
#   never merges with or deletes unrelated content the target dir might
#   already hold for a different skill. Skipped entirely when HOME is empty/
#   unset and no --skills-dir/CLAGENTIC_LOADOUT_SKILLS_DIR override is given
#   (same empty-HOME fail-fast discipline as every other HOME-derived default
#   in this script, but soft here -- a caller uninterested in skill install at
#   all is not blocked from installing the console_scripts).
#
# Git-host config seeding:
#   This installer seeds ~/.config/clagentic/loadout/config.yaml's
#   `git_host: base_url:` key -- the config-file tier
#   transport.git_host_api._resolve_git_host_base reads as its lowest-priority
#   non-placeholder source (see docs/integration.md). This is the RELEASED
#   mechanism for supplying a git-host base URL: install once, and no
#   deployment or spawn env has to hand-export an env var.
#
#   --git-host-base-url URL (or CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL) supplies
#   the value explicitly. No hardcoded operator host is ever baked into this
#   script (CLAUDE.md rule 1) -- if neither is given, a clearly-commented
#   TEMPLATE entry is written for the user to fill in, never a dead
#   localhost and never a baked URL.
#
#   Idempotent: the config dir/file are created if absent (mode 700/600 --
#   this file can carry deployment-identifying config even though this
#   particular key is not a secret). An EXISTING git_host.base_url that is
#   not the commented-out template is NEVER overwritten without an explicit
#   --git-host-base-url this run -- a prior real value always wins over a
#   silent template re-write.

set -eu

PROG="install.sh"
EXIT_OK=0
EXIT_USAGE=1
EXIT_NO_INSTALLER=2
EXIT_INSTALL_FAILED=3

SOURCE_PATH=""
INSTALLER_OVERRIDE=""
EDITABLE=0
DRY_RUN=0
# Additional bin dirs the caller wants checked/reported for PATH visibility,
# on top of whatever the chosen installer's own default target is. Space-
# separated accumulation (POSIX sh has no arrays); repeatable --path-dir.
EXTRA_PATH_DIRS=""
# Base dir for the self-managed venv tier. Only consulted when the
# venv installer is selected; overridable via --data-dir or
# CLAGENTIC_LOADOUT_HOME.
DATA_DIR=""
# Symlink-target bin dir for the venv tier's console_scripts. Only
# consulted when the venv installer is selected; overridable via --bin-dir or
# CLAGENTIC_LOADOUT_BIN_DIR. Mirrors PIPX_BIN_DIR/UV_TOOL_BIN_DIR, which the
# pipx/uv tiers already had -- the venv tier had no equivalent override for
# its own HOME-derived symlink-target dir until this fix.
BIN_DIR=""
# Git-host base URL to seed into config.yaml's git_host.base_url key.
# Overridable via --git-host-base-url or
# CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL. Empty means "write the commented
# template instead" -- see _seed_git_host_config below. Never a hardcoded
# operator host (CLAUDE.md rule 1).
GIT_HOST_BASE_URL=""
# Target dir for the global skill install. Overridable via
# --skills-dir or CLAGENTIC_LOADOUT_SKILLS_DIR. Empty means the HOME-derived
# default, ~/.claude/skills -- see _install_skills below. Never a hardcoded
# operator path (CLAUDE.md rule 1).
SKILLS_DIR=""

_usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Options:
  --source PATH        Install from this sdist/wheel/checkout path instead of
                        the checkout install.sh itself lives in.
  --installer NAME      Force a specific installer: pipx, uv, pip, or venv.
                        Default: auto-detect, preferring pipx, then uv, then
                        pip --user, then a self-managed venv (the last-resort
                        tier for PEP 668 externally-managed environments with
                        no pipx/uv).
  --editable            Editable/dev install where the chosen installer
                        supports it (pip/venv: -e; pipx/uv: passed through as
                        their own editable-install flag).
  --path-dir DIR         An additional console-script directory to verify/
                        report for PATH visibility (repeatable).
  --data-dir DIR         Base dir for the self-managed venv tier (default:
                        ~/.local/share/clagentic/loadout, override via
                        CLAGENTIC_LOADOUT_HOME). Ignored by the other tiers.
  --bin-dir DIR          Symlink-target bin dir for the self-managed venv
                        tier's console_scripts (default: ~/.local/bin,
                        override via CLAGENTIC_LOADOUT_BIN_DIR). Ignored by
                        the other tiers (pipx/uv have their own
                        PIPX_BIN_DIR/UV_TOOL_BIN_DIR).
  --git-host-base-url URL
                        Seed ~/.config/clagentic/loadout/config.yaml's
                        git_host.base_url key (override via
                        CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL). Never
                        overwrites an existing real value without this flag
                        being passed again. Omit to write a commented
                        template instead of a value.
  --skills-dir DIR       Target dir for this repo's global .claude/skills/
                        install (default: ~/.claude/skills, override via
                        CLAGENTIC_LOADOUT_SKILLS_DIR). Each shipped skill is
                        copied to <dir>/<skill-name>/, overwriting only that
                        skill's own subdirectory.
  --dry-run              Print the resolved installer + command, install
                        nothing.
  -h, --help             Show this help and exit.
  --version              Show this script's own version marker and exit.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source)
            [ "$#" -ge 2 ] || { echo "$PROG: --source requires a PATH argument" >&2; exit "$EXIT_USAGE"; }
            SOURCE_PATH="$2"
            shift 2
            ;;
        --installer)
            [ "$#" -ge 2 ] || { echo "$PROG: --installer requires an argument (pipx|uv|pip)" >&2; exit "$EXIT_USAGE"; }
            INSTALLER_OVERRIDE="$2"
            shift 2
            ;;
        --editable)
            EDITABLE=1
            shift
            ;;
        --path-dir)
            [ "$#" -ge 2 ] || { echo "$PROG: --path-dir requires a DIR argument" >&2; exit "$EXIT_USAGE"; }
            EXTRA_PATH_DIRS="$EXTRA_PATH_DIRS $2"
            shift 2
            ;;
        --data-dir)
            [ "$#" -ge 2 ] || { echo "$PROG: --data-dir requires a DIR argument" >&2; exit "$EXIT_USAGE"; }
            DATA_DIR="$2"
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || { echo "$PROG: --bin-dir requires a DIR argument" >&2; exit "$EXIT_USAGE"; }
            BIN_DIR="$2"
            shift 2
            ;;
        --git-host-base-url)
            [ "$#" -ge 2 ] || { echo "$PROG: --git-host-base-url requires a URL argument" >&2; exit "$EXIT_USAGE"; }
            GIT_HOST_BASE_URL="$2"
            shift 2
            ;;
        --skills-dir)
            [ "$#" -ge 2 ] || { echo "$PROG: --skills-dir requires a DIR argument" >&2; exit "$EXIT_USAGE"; }
            SKILLS_DIR="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            _usage
            exit "$EXIT_OK"
            ;;
        --version)
            echo "$PROG (clagentic-loadout installer)"
            exit "$EXIT_OK"
            ;;
        *)
            echo "$PROG: unknown argument: $1" >&2
            _usage >&2
            exit "$EXIT_USAGE"
            ;;
    esac
done

# Env-var overrides apply only when the corresponding flag was not given.
if [ -z "$INSTALLER_OVERRIDE" ] && [ -n "${CLAGENTIC_LOADOUT_INSTALLER:-}" ]; then
    INSTALLER_OVERRIDE="$CLAGENTIC_LOADOUT_INSTALLER"
fi
if [ -z "$SOURCE_PATH" ] && [ -n "${CLAGENTIC_LOADOUT_SOURCE:-}" ]; then
    SOURCE_PATH="$CLAGENTIC_LOADOUT_SOURCE"
fi
if [ -z "$DATA_DIR" ] && [ -n "${CLAGENTIC_LOADOUT_HOME:-}" ]; then
    DATA_DIR="$CLAGENTIC_LOADOUT_HOME"
fi
if [ -z "$BIN_DIR" ] && [ -n "${CLAGENTIC_LOADOUT_BIN_DIR:-}" ]; then
    BIN_DIR="$CLAGENTIC_LOADOUT_BIN_DIR"
fi
if [ -z "$GIT_HOST_BASE_URL" ] && [ -n "${CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL:-}" ]; then
    GIT_HOST_BASE_URL="$CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL"
fi
if [ -z "$SKILLS_DIR" ] && [ -n "${CLAGENTIC_LOADOUT_SKILLS_DIR:-}" ]; then
    SKILLS_DIR="$CLAGENTIC_LOADOUT_SKILLS_DIR"
fi

# Trim leading/trailing whitespace from path-shaped values BEFORE the
# emptiness guards below run --
# a whitespace-only "--data-dir ' '" previously passed every [ -n ... ]
# check and went on to `mkdir -p` a literal whitespace directory. Trimming
# first means a whitespace-only value is correctly treated as "not given".
_trim() {
    # POSIX-portable trim via word-splitting through $* -- deliberately no
    # sed/awk dependency for a two-line operation this small.
    # shellcheck disable=SC2048,SC2086
    set -- $1
    echo "$*"
}
SOURCE_PATH="$(_trim "$SOURCE_PATH")"
DATA_DIR="$(_trim "$DATA_DIR")"
BIN_DIR="$(_trim "$BIN_DIR")"
GIT_HOST_BASE_URL="$(_trim "$GIT_HOST_BASE_URL")"
SKILLS_DIR="$(_trim "$SKILLS_DIR")"

# Fail fast on empty/unset HOME rather than silently resolving
# ${HOME:-}/.local/... down to root-relative paths (/.local/bin,
# /.local/share/clagentic/loadout) -- `set -u` alone does not catch this,
# since ${HOME:-} is a deliberately guarded expansion that degrades to an
# empty string instead of erroring, and every one of this script's
# HOME-derived defaults (DATA_DIR here, plus each installer tier's own
# DEFAULT_BIN_DIR: PIPX_BIN_DIR/UV_TOOL_BIN_DIR/pip's sysconfig lookup/the
# venv tier, all below) inherits that empty string silently.
#
# An agent-spawn environment that never sets HOME is a real, expected
# caller (not a misconfiguration), so this only fails when HOME is empty
# AND nothing else was given that would let every HOME-derived resolution
# below be replaced.
#
# Rule: DATA_DIR compensation is NOT sufficient by itself whenever
# the venv tier could still be SELECTED this run -- i.e. INSTALLER_OVERRIDE
# is unset (auto-detect can still land on venv, e.g. PEP 668 with no pipx/
# uv) or is explicitly "venv". When it is explicitly "pip", "pipx", or "uv"
# instead, the venv tier is provably unreachable this run (the case
# statement below only builds the forced tier's command), so no bin-dir
# compensation is required for it here -- pip's own DEFAULT_BIN_DIR
# resolution and pipx/uv's PIPX_BIN_DIR/UV_TOOL_BIN_DIR are each that tier's
# own concern, not this one.
#
# When the venv tier IS reachable, its bin-dir must ALSO be compensated, via
# --bin-dir/CLAGENTIC_LOADOUT_BIN_DIR (BIN_DIR here) -- otherwise --data-dir
# alone passes this guard while `mkdir -p "$DEFAULT_BIN_DIR"` further down
# still creates a root-relative /.local/bin, the exact hazard class the
# fail-fast guard above closed for DATA_DIR itself.
case "$INSTALLER_OVERRIDE" in
    ""|venv)
        _venv_tier_reachable=1
        ;;
    *)
        # "pip", "pipx", "uv", or any other value the later validation will
        # reject on its own merits -- none of these resolve to the venv
        # tier's HOME-derived DEFAULT_BIN_DIR this run.
        _venv_tier_reachable=0
        ;;
esac

if [ -z "${HOME:-}" ] \
    && [ -z "$DATA_DIR" ] \
    && [ -z "${PIPX_BIN_DIR:-}" ] \
    && [ -z "${UV_TOOL_BIN_DIR:-}" ]; then
    echo "$PROG: HOME is empty or unset, and no override was given to compensate." >&2
    echo "$PROG: resolved: HOME='${HOME:-}' DATA_DIR='$DATA_DIR' BIN_DIR='$BIN_DIR' PIPX_BIN_DIR='${PIPX_BIN_DIR:-}' UV_TOOL_BIN_DIR='${UV_TOOL_BIN_DIR:-}' installer_override='${INSTALLER_OVERRIDE:-<auto>}'" >&2
    echo "$PROG: refusing to fall back to root-relative paths (e.g. /.local/bin," \
        "/.local/share/clagentic/loadout) -- that succeeds silently while" \
        "installing nowhere useful, which is worse than failing here." >&2
    echo "$PROG: fix one PER-TIER override (only the tier you will actually use" \
        "needs compensating): venv tier -- --data-dir DIR (or" \
        "CLAGENTIC_LOADOUT_HOME) PLUS --bin-dir DIR (or CLAGENTIC_LOADOUT_BIN_DIR," \
        "both required, see the next check below); pipx tier -- PIPX_BIN_DIR;" \
        "uv tier -- UV_TOOL_BIN_DIR; pip tier -- no override exists here, set" \
        "HOME to a real, writable directory instead. Auto-detect (no" \
        "--installer) can still land on the venv tier, so the venv-tier" \
        "compensation is required unless --installer pipx/uv/pip is forced." >&2
    exit "$EXIT_USAGE"
fi

if [ -z "${HOME:-}" ] \
    && [ -n "$DATA_DIR" ] \
    && [ -z "$BIN_DIR" ] \
    && [ "$_venv_tier_reachable" -eq 1 ]; then
    echo "$PROG: HOME is empty or unset; --data-dir/CLAGENTIC_LOADOUT_HOME was" \
        "given but that only compensates the venv tier's own venv base dir," \
        "not its symlink-target bin dir." >&2
    echo "$PROG: resolved: HOME='${HOME:-}' DATA_DIR='$DATA_DIR' BIN_DIR='$BIN_DIR' installer_override='${INSTALLER_OVERRIDE:-<auto>}'" >&2
    echo "$PROG: the venv tier's DEFAULT_BIN_DIR still falls back to" \
        "\"\${HOME:-}/.local/bin\" -- a root-relative path -- unless --bin-dir" \
        "DIR (or CLAGENTIC_LOADOUT_BIN_DIR) is also given." >&2
    echo "$PROG: fix one of: pass --bin-dir DIR (or CLAGENTIC_LOADOUT_BIN_DIR);" \
        "set HOME to a real, writable directory; force --installer pipx or" \
        "--installer uv with PIPX_BIN_DIR/UV_TOOL_BIN_DIR set instead." >&2
    exit "$EXIT_USAGE"
fi

if [ -z "$DATA_DIR" ]; then
    DATA_DIR="${HOME:-}/.local/share/clagentic/loadout"
fi
if [ "$EDITABLE" -eq 0 ] && [ -n "${PIP_EDITABLE:-}" ] && [ "${PIP_EDITABLE:-0}" != "0" ]; then
    EDITABLE=1
fi

# Default source: the checkout this script lives in (scripts/.. == repo root).
if [ -z "$SOURCE_PATH" ]; then
    _script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    SOURCE_PATH="$(CDPATH= cd -- "$_script_dir/.." && pwd)"
fi

if [ ! -e "$SOURCE_PATH" ]; then
    echo "$PROG: --source path does not exist: $SOURCE_PATH" >&2
    exit "$EXIT_USAGE"
fi

# ---------------------------------------------------------------------------
# Installer detection: pipx > uv > pip --user > venv.
#
# pipx is preferred because it installs each Python CLI application into its
# own isolated venv and symlinks only the console_scripts onto a single,
# well-known bin directory (~/.local/bin by default) -- exactly the PATH-
# wiring guarantee this script exists to make. uv is a close second (same
# isolation property via `uv tool install`). Bare `pip install --user` is
# the least isolated option (shares site-packages with anything else the
# user pip-installed) but was, until the venv tier was added, the universal
# last-resort fallback.
#
# It no longer is: PEP 668 "externally-managed-environment" systems (the
# system Python on stock Debian/Ubuntu, among others) refuse `pip install
# --user` outright, and pipx/uv are not always present to route around it
# (they're commonly installed FROM pip in the first place, so their absence
# and PEP 668's presence tend to travel together). The venv tier below is
# the terminal fallback for that case: a self-managed virtualenv this
# script owns end to end, with console_scripts symlinked out to a normal
# PATH directory.
#
# Trade-off named per conformance rule 4 / the PR body: PEP 668 is detected
# PROACTIVELY, by checking for the interpreter's own EXTERNALLY-MANAGED
# marker file (PEP 668 sec. "Marking an environment as externally managed"),
# rather than by pattern-matching pip's stderr for the string
# "externally-managed-environment" after a failed install attempt. Sniffing
# stderr text is fragile against pip version/locale changes and would mean
# discovering the failure only after already having tried and failed;
# checking the marker is a cheap, stable, versioned part of the interpreter
# distribution itself, and lets the plan be resolved (and printed) before
# any command runs -- consistent with "no stale guesses" in error/plan
# reporting.
# ---------------------------------------------------------------------------

_have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Resolve the python3 interpreter used for detection/venv creation once, so
# every check below (marker probe, venv creation, sysconfig lookups) agrees
# on the same interpreter.
_py() {
    if _have_cmd python3; then
        echo python3
    elif _have_cmd python; then
        echo python
    else
        return 1
    fi
}

# Cheap, proactive PEP 668 probe: does this interpreter's stdlib directory
# carry the EXTERNALLY-MANAGED marker file? No install attempt required.
_pep668_externally_managed() {
    _py_bin="$(_py)" || return 1
    _stdlib_dir="$("$_py_bin" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))' 2>/dev/null)" || return 1
    [ -n "$_stdlib_dir" ] && [ -f "$_stdlib_dir/EXTERNALLY-MANAGED" ]
}

# Diagnostic trail for the plan/failure report -- conformance rule 4
# (resolved values, not stale guesses): which tiers were probed and what was
# found/missing for each. Written directly to stderr as detection proceeds
# (rather than buffered and replayed later) so it survives unchanged
# whether _detect_installer is called from a plain command or a $(...)
# capture -- no data has to cross the command-substitution boundary except
# the one value that actually needs to (the chosen tier's name, via stdout).
_probe() {
    echo "$PROG: probe: $1" >&2
}

_detect_installer() {
    if [ -n "$INSTALLER_OVERRIDE" ]; then
        echo "$INSTALLER_OVERRIDE"
        return 0
    fi
    if _have_cmd pipx; then
        _probe "pipx: found ($(command -v pipx))"
        echo "pipx"
        return 0
    fi
    _probe "pipx: not found on PATH"
    if _have_cmd uv; then
        _probe "uv: found ($(command -v uv))"
        echo "uv"
        return 0
    fi
    _probe "uv: not found on PATH"
    if _have_cmd pip3 || _have_cmd pip; then
        if _pep668_externally_managed; then
            _probe "pip: found ($(command -v pip3 || command -v pip)), but interpreter is externally-managed (PEP 668 marker present) -- skipping to venv tier"
        else
            _probe "pip: found ($(command -v pip3 || command -v pip)), no PEP 668 marker detected"
            echo "pip"
            return 0
        fi
    else
        _probe "pip3/pip: not found on PATH"
    fi
    if _py >/dev/null 2>&1; then
        _probe "venv: $(_py) available, using self-managed venv tier"
        echo "venv"
        return 0
    fi
    _probe "venv: no python3/python interpreter found to create one"
    return 1
}

_DETECT_STATUS=0
INSTALLER="$(_detect_installer)" || _DETECT_STATUS=$?

if [ "$_DETECT_STATUS" -ne 0 ]; then
    echo "$PROG: no supported installer found (see probe results above)." >&2
    echo "$PROG: install one of these first, e.g. 'python3 -m pip install --user pipx' (if not externally-managed) or ensure a python3 interpreter is on PATH for the venv tier." >&2
    exit "$EXIT_NO_INSTALLER"
fi

case "$INSTALLER" in
    pipx|uv|pip|venv) ;;
    *)
        echo "$PROG: --installer must be one of: pipx, uv, pip, venv (got: $INSTALLER)" >&2
        exit "$EXIT_USAGE"
        ;;
esac

# ---------------------------------------------------------------------------
# Build the install command for the resolved installer.
# ---------------------------------------------------------------------------

_pip_bin() {
    if _have_cmd pip3; then
        echo pip3
    else
        echo pip
    fi
}

case "$INSTALLER" in
    pipx)
        if [ "$EDITABLE" -eq 1 ]; then
            set -- pipx install --force --editable "$SOURCE_PATH"
        else
            set -- pipx install --force "$SOURCE_PATH"
        fi
        DEFAULT_BIN_DIR="${PIPX_BIN_DIR:-${HOME:-}/.local/bin}"
        ;;
    uv)
        if [ "$EDITABLE" -eq 1 ]; then
            set -- uv tool install --force --editable "$SOURCE_PATH"
        else
            set -- uv tool install --force "$SOURCE_PATH"
        fi
        DEFAULT_BIN_DIR="${UV_TOOL_BIN_DIR:-${HOME:-}/.local/bin}"
        ;;
    pip)
        _pip="$(_pip_bin)"
        if [ "$EDITABLE" -eq 1 ]; then
            set -- "$_pip" install --user --editable "$SOURCE_PATH"
        else
            set -- "$_pip" install --user "$SOURCE_PATH"
        fi
        # `pip install --user`'s console_scripts land in the interpreter's
        # user base bin dir; ask the SAME interpreter pip is attached to,
        # rather than assuming a fixed path, since --user's base varies by
        # platform/venv configuration.
        DEFAULT_BIN_DIR="$("$_pip" show pip >/dev/null 2>&1 && "$(command -v python3 || command -v python)" -c 'import sysconfig; print(sysconfig.get_path("scripts", f"{sysconfig.get_default_scheme()}_user"))' 2>/dev/null || echo "${HOME:-}/.local/bin")"
        ;;
    venv)
        # Self-managed, idempotent venv tier: a virtualenv this
        # script owns end to end, under DATA_DIR/venv. Re-running installs
        # into the SAME venv (pip upgrades in place); nothing here ever
        # passes --break-system-packages -- this tier exists precisely to
        # avoid needing that flag.
        VENV_DIR="$DATA_DIR/venv"
        _py_bin="$(_py)" || {
            echo "$PROG: venv tier selected but no python3/python interpreter is on PATH." >&2
            exit "$EXIT_NO_INSTALLER"
        }
        if [ ! -x "$VENV_DIR/bin/python3" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
            echo "$PROG: creating venv at $VENV_DIR" >&2
            "$_py_bin" -m venv "$VENV_DIR"
        else
            echo "$PROG: reusing existing venv at $VENV_DIR" >&2
        fi
        _venv_pip="$VENV_DIR/bin/pip"
        if [ "$EDITABLE" -eq 1 ]; then
            set -- "$_venv_pip" install --upgrade --editable "$SOURCE_PATH"
        else
            set -- "$_venv_pip" install --upgrade "$SOURCE_PATH"
        fi
        # Console scripts land in the venv's own bin dir; this tier then
        # symlinks them out to DEFAULT_BIN_DIR (a normal, already-on-PATH
        # user bin dir) rather than asking the caller to add the venv's bin
        # dir itself to PATH -- keeping this tier's PATH story identical to
        # pipx/uv's (a single well-known bin dir with symlinks in it).
        # BIN_DIR (--bin-dir/CLAGENTIC_LOADOUT_BIN_DIR) overrides
        # this HOME-derived default, mirroring PIPX_BIN_DIR/UV_TOOL_BIN_DIR
        # above -- the fail-fast guard already required this compensation
        # when HOME is empty, so by the time this line runs either HOME is
        # real or BIN_DIR is set.
        DEFAULT_BIN_DIR="${BIN_DIR:-${HOME:-}/.local/bin}"
        ;;
esac

echo "$PROG: installer=$INSTALLER source=$SOURCE_PATH editable=$EDITABLE" >&2
if [ "$INSTALLER" = "venv" ]; then
    echo "$PROG: venv-dir=$VENV_DIR" >&2
fi
echo "$PROG: command: $*" >&2

if [ "$DRY_RUN" -eq 1 ]; then
    echo "$PROG: --dry-run -- not executing." >&2
    exit "$EXIT_OK"
fi

if ! "$@"; then
    echo "$PROG: install command failed (installer=$INSTALLER)." >&2
    exit "$EXIT_INSTALL_FAILED"
fi

# ---------------------------------------------------------------------------
# venv tier only: symlink the venv's console_scripts out to DEFAULT_BIN_DIR
# Idempotent: stale symlinks that already point into THIS venv's
# bin dir are replaced; a pre-existing file/symlink that points somewhere
# ELSE is left alone and reported, never clobbered.
#
# The script list is read from pyproject.toml's [project.scripts] table
# (parsed with a small POSIX-sh state machine -- no TOML library available
# in plain sh) so it never drifts from the package's own declared entry
# points; a hardcoded fallback list covers the case where SOURCE_PATH isn't
# a checkout with a readable pyproject.toml (e.g. a bare sdist/wheel path).
# ---------------------------------------------------------------------------

_FALLBACK_SCRIPT_NAMES="clagentic-loadout loadout-poll-wait loadout-scoped-test-wait loadout-release-dispatch loadout-release-detect loadout-git-host-api loadout-review-post loadout-push loadout-merge loadout-provision-allowlist"

_project_script_names() {
    _pyproject="$1/pyproject.toml"
    [ -r "$_pyproject" ] || return 1
    awk '
        /^\[project\.scripts\]/ { in_section=1; next }
        /^\[/ { in_section=0 }
        in_section && /^[A-Za-z0-9_-]+[ \t]*=/ {
            sub(/[ \t]*=.*/, "");
            gsub(/[ \t]/, "");
            print;
        }
    ' "$_pyproject"
}

if [ "$INSTALLER" = "venv" ]; then
    SCRIPT_NAMES="$(_project_script_names "$SOURCE_PATH" 2>/dev/null || true)"
    if [ -z "$SCRIPT_NAMES" ]; then
        SCRIPT_NAMES="$_FALLBACK_SCRIPT_NAMES"
    fi

    mkdir -p "$DEFAULT_BIN_DIR"

    _symlink_failures=""
    for _name in $SCRIPT_NAMES; do
        _venv_script="$VENV_DIR/bin/$_name"
        _target="$DEFAULT_BIN_DIR/$_name"
        if [ ! -x "$_venv_script" ]; then
            # Not every declared script is necessarily installed by every
            # partial/editable build; skip silently rather than fail the
            # whole run over one missing entry point.
            continue
        fi
        if [ -L "$_target" ]; then
            _existing_dest="$(readlink "$_target" 2>/dev/null || true)"
            if [ "$_existing_dest" = "$_venv_script" ]; then
                continue
            fi
            # A symlink we don't recognize as ours -- replace only if it
            # points elsewhere INTO the same venv dir (a stale prior
            # install); otherwise leave it and report.
            case "$_existing_dest" in
                "$VENV_DIR"/*)
                    rm -f "$_target"
                    ln -s "$_venv_script" "$_target"
                    ;;
                *)
                    _symlink_failures="$_symlink_failures $_target(existing-symlink->$_existing_dest)"
                    ;;
            esac
        elif [ -e "$_target" ]; then
            _symlink_failures="$_symlink_failures $_target(existing-non-symlink-file)"
        else
            ln -s "$_venv_script" "$_target"
        fi
    done

    if [ -n "$_symlink_failures" ]; then
        echo "$PROG: could not refresh symlink(s), left untouched (not owned by this installer):$_symlink_failures" >&2
    fi
fi

# ---------------------------------------------------------------------------
# PATH verification + repair (retiring a calling harness's own PATH-prepend
# workaround step).
#
# Checks every candidate console-script bin dir (the installer's own default
# plus any --path-dir overrides) against the CURRENT PATH. If none are on
# PATH, this script does not silently leave the caller broken: it prints an
# explicit, copy-pasteable export line for the user's shell rc file AND, for
# a Bourne-compatible interactive shell sourcing this script directly rather
# than executing it, exports PATH into the current shell.
# ---------------------------------------------------------------------------

_dir_on_path() {
    _dir="$1"
    case ":$PATH:" in
        *":$_dir:"*) return 0 ;;
        *) return 1 ;;
    esac
}

_candidate_dirs="$DEFAULT_BIN_DIR$EXTRA_PATH_DIRS"
_missing_dirs=""
for _dir in $_candidate_dirs; do
    if [ -d "$_dir" ] && ! _dir_on_path "$_dir"; then
        _missing_dirs="$_missing_dirs $_dir"
    fi
done

if [ -n "$_missing_dirs" ]; then
    echo "$PROG: console-script directory not on PATH:$_missing_dirs" >&2
    for _dir in $_missing_dirs; do
        echo "  export PATH=\"$_dir:\$PATH\""
    done
    echo "$PROG: add the export(s) above to your shell rc file (~/.bashrc," \
        "~/.zshrc, or an agent-spawn environment's own init script) so" \
        "console_scripts resolve in EVERY subsequent shell, not just this" \
        "one. If this script was sourced (not executed) in the current" \
        "shell, PATH has already been updated for THIS session only." >&2
    for _dir in $_missing_dirs; do
        PATH="$_dir:$PATH"
    done
    export PATH
else
    echo "$PROG: console-script directory already on PATH: $DEFAULT_BIN_DIR" >&2
fi

# ---------------------------------------------------------------------------
# Skill install: copy this repo's global, LLM-invocable skills
# (.claude/skills/<skill-name>/) out to a HOME-derived skills dir, so a
# harness that discovers skills from e.g. ~/.claude/skills/ picks them up
# without a manual copy step -- the same "make the thing actually reachable
# after install" guarantee this script already provides for console_scripts
# on PATH, applied to skill discovery instead.
#
# Target dir resolution, highest precedence first: --skills-dir,
# CLAGENTIC_LOADOUT_SKILLS_DIR, then a HOME-derived default
# (~/.claude/skills). No hardcoded operator path anywhere (CLAUDE.md rule 1).
#
# Skipped (not a hard failure) when HOME is empty/unset and no --skills-dir/
# CLAGENTIC_LOADOUT_SKILLS_DIR override was given -- a caller that only
# wants the console_scripts installed, in an environment with no usable
# HOME, is not blocked by this optional step.
#
# Idempotent: each shipped skill is copied wholesale into
# <skills-dir>/<skill-name>/, overwriting only that skill's own
# subdirectory (a plain recursive copy, mirroring the venv tier's own
# "re-running installs into the SAME target" contract) -- never touching any
# OTHER skill subdirectory that may already live under the same skills dir.
#
# Runs only when the actual install proceeds -- --dry-run exits earlier
# (right after printing the resolved plan, before the install command
# itself runs), so this step is unreachable in a dry-run invocation and
# needs no separate dry-run guard of its own.
# ---------------------------------------------------------------------------

_SKILLS_SOURCE_ROOT="$SOURCE_PATH/.claude/skills"
_SHIPPED_SKILL_NAMES="loadout-init"

_install_skills() {
    if [ ! -d "$_SKILLS_SOURCE_ROOT" ]; then
        echo "$PROG: no .claude/skills/ found under $SOURCE_PATH -- skipping skill install." >&2
        return 0
    fi

    _resolved_skills_dir="$SKILLS_DIR"
    if [ -z "$_resolved_skills_dir" ]; then
        if [ -z "${HOME:-}" ]; then
            echo "$PROG: HOME is empty or unset and no --skills-dir/CLAGENTIC_LOADOUT_SKILLS_DIR override was given -- skipping skill install (console_scripts are unaffected)." >&2
            return 0
        fi
        _resolved_skills_dir="${HOME:-}/.claude/skills"
    fi

    mkdir -p "$_resolved_skills_dir"

    for _skill_name in $_SHIPPED_SKILL_NAMES; do
        _skill_source="$_SKILLS_SOURCE_ROOT/$_skill_name"
        if [ ! -d "$_skill_source" ]; then
            echo "$PROG: shipped skill $_skill_name not found at $_skill_source -- skipping." >&2
            continue
        fi
        _skill_target="$_resolved_skills_dir/$_skill_name"
        rm -rf "$_skill_target"
        cp -R "$_skill_source" "$_skill_target"
        echo "$PROG: installed skill $_skill_name -> $_skill_target" >&2
    done
}

_install_skills

# ---------------------------------------------------------------------------
# Git-host config seeding: write ~/.config/clagentic/loadout/
# config.yaml's `git_host: base_url:` key -- the config-file tier
# transport.git_host_api._resolve_git_host_base reads (see docs/integration.md
# and that module's GIT_HOST_CONFIG_SECTION/GIT_HOST_CONFIG_KEY_BASE_URL
# constants, which this MUST stay in lockstep with).
#
# Idempotent, never clobbers a real existing value without --git-host-base-
# url/CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL being supplied THIS run: if
# config.yaml already has a git_host.base_url that is not the commented-out
# template, and no explicit URL was given this run, the file is left
# untouched. When GIT_HOST_BASE_URL is empty and there is no prior real
# value, a clearly-commented TEMPLATE line is written (never a dead
# localhost, never a baked operator host -- CLAUDE.md rule 1) so a
# subsequent hand-edit is a one-line uncomment+fill, not archaeology.
#
# Safety (write path is new; this is a NEW filesystem-write surface): the
# config dir is created mode 700 and the file mode 600 (this file is not
# secret-bearing today, but co-locating with a possible future
# credentials-adjacent key under the same root argues for the tighter
# default rather than loosening it later). GIT_HOST_BASE_URL is written as
# a single-quoted YAML scalar with any embedded single quote escaped per
# YAML rules (doubled) -- it is never interpolated into a shell command, so
# a hostile flag value cannot achieve shell injection here.
#
# Symlink handling, READ and WRITE sides (security-review finding): a
# SYMLINK found at the target config path is refused outright, before either
# side runs --
# `_target_is_symlink` below gates BOTH (1) the pre-existing-content read
# that preserves other top-level sections, via awk, and (2) the final
# replace. This function does not read through, resolve-and-validate, or
# write through a symlink at $_CONFIG_FILE under any circumstance; a
# symlink there is a refuse-and-report condition, full stop. (An EARLIER
# version of this comment claimed the write side alone was symlink-safe via
# `rm -f` unlink-before-rename -- true for the WRITE, but that framing
# undersold that the READ side, which runs first to preserve existing
# sections, had no equivalent guard. Both sides are now covered by the same
# up-front refusal.)
# ---------------------------------------------------------------------------

_CONFIG_ROOT="${HOME:-}/.config/clagentic/loadout"
_CONFIG_FILE="$_CONFIG_ROOT/config.yaml"
_GIT_HOST_TEMPLATE_MARKER="# TEMPLATE -- fill in your git host's base URL and uncomment, e.g. https://git.example.com"

_yaml_single_quote() {
    # Escape a value for a YAML single-quoted scalar: only a literal single
    # quote needs escaping (doubled), no backslash-escape processing in
    # single-quoted YAML -- so this is deliberately narrower than a general
    # shell-quoting helper.
    printf '%s' "$1" | sed "s/'/''/g"
}

_seed_git_host_config() {
    if [ -z "${HOME:-}" ]; then
        echo "$PROG: HOME is empty or unset -- skipping git-host config seeding (nothing writable to seed under)." >&2
        return 0
    fi

    # Security-review finding: refuse a SYMLINK at the target path OUTRIGHT,
    # before any read or write. `-L` is checked FIRST and independently of `-f`/
    # `-e` -- POSIX `-f`/`-e` FOLLOW a symlink to test the file it points
    # at, so a symlink pointing at a regular file would otherwise pass a
    # "-e && -f" style guard silently, and its target's contents would then
    # be read (to preserve existing sections) before this function unlinks
    # and replaces whatever sits at $_CONFIG_FILE -- a local TOCTOU /
    # confused-deputy read, distinct from (and in addition to) the write-
    # side unlink-before-rename protection below. No resolve-and-validate
    # fallback is offered: a symlink at this path is never expected in
    # normal use, so refusing outright is not a usability regression.
    if [ -L "$_CONFIG_FILE" ]; then
        echo "$PROG: $_CONFIG_FILE is a symlink -- refusing to read through or write through it. Remove the symlink and re-run (there is no option to redirect the write path; edit $_CONFIG_FILE by hand instead if needed)." >&2
        return 0
    fi

    if [ -e "$_CONFIG_FILE" ] && [ ! -f "$_CONFIG_FILE" ]; then
        echo "$PROG: $_CONFIG_FILE exists and is not a regular file -- refusing to touch it." >&2
        return 0
    fi

    _existing_real_value=""
    if [ -f "$_CONFIG_FILE" ]; then
        # A real (non-template, non-comment) base_url line under a git_host:
        # section. Deliberately simple line-oriented parsing (no YAML
        # library in plain sh) -- good enough to detect "does a real value
        # already exist," which is all this idempotency check needs; the
        # actual read at resolve time goes through the real YAML loader in
        # provider_config.load_user_config_section.
        _existing_real_value="$(awk '
            /^git_host:[ \t]*$/ { in_section=1; next }
            /^[A-Za-z_][A-Za-z0-9_]*:/ { in_section=0 }
            in_section && /^[ \t]+base_url:[ \t]*/ && !/^[ \t]*#/ {
                sub(/^[ \t]+base_url:[ \t]*/, "");
                if ($0 !~ /^[ \t]*$/) print;
            }
        ' "$_CONFIG_FILE" | head -n1)"
    fi

    if [ -n "$_existing_real_value" ] && [ -z "$GIT_HOST_BASE_URL" ]; then
        echo "$PROG: $_CONFIG_FILE already has a git_host.base_url set -- leaving it untouched (pass --git-host-base-url to replace it)." >&2
        return 0
    fi

    mkdir -p -m 700 "$_CONFIG_ROOT"

    if [ -n "$GIT_HOST_BASE_URL" ]; then
        _quoted="$(_yaml_single_quote "$GIT_HOST_BASE_URL")"
        _base_url_line="  base_url: '$_quoted'"
        _seed_msg="seeded git_host.base_url"
    else
        _base_url_line="  $_GIT_HOST_TEMPLATE_MARKER
  # base_url: 'https://git.example.com'"
        _seed_msg="wrote a commented git_host.base_url TEMPLATE (no value supplied -- pass --git-host-base-url or edit $_CONFIG_FILE directly)"
    fi

    if [ -f "$_CONFIG_FILE" ]; then
        # Preserve any OTHER top-level section already in the file; only
        # the git_host: section is rewritten. Simple line-oriented rewrite
        # (drop the old git_host: section, append the new one) -- avoids a
        # YAML-parse-then-reserialize round trip that could reformat
        # unrelated sections' comments/ordering.
        _tmp_rewrite="$_CONFIG_ROOT/.config.yaml.rewrite.$$"
        rm -f "$_tmp_rewrite"
        : > "$_tmp_rewrite"
        chmod 600 "$_tmp_rewrite"
        awk '
            /^git_host:[ \t]*$/ { in_section=1; next }
            /^[A-Za-z_][A-Za-z0-9_]*:/ { in_section=0 }
            !in_section { print }
        ' "$_CONFIG_FILE" >> "$_tmp_rewrite"
        {
            echo "git_host:"
            echo "$_base_url_line"
        } >> "$_tmp_rewrite"
    else
        _tmp_rewrite="$_CONFIG_ROOT/.config.yaml.rewrite.$$"
        rm -f "$_tmp_rewrite"
        {
            echo "git_host:"
            echo "$_base_url_line"
        } > "$_tmp_rewrite"
        chmod 600 "$_tmp_rewrite"
    fi

    # Break any pre-existing symlink/file at the target path before the
    # atomic rename, rather than opening/writing through it -- a symlink
    # planted at $_CONFIG_FILE by another process/user is never followed.
    rm -f "$_CONFIG_FILE"
    mv "$_tmp_rewrite" "$_CONFIG_FILE"
    chmod 600 "$_CONFIG_FILE"

    echo "$PROG: $_seed_msg ($_CONFIG_FILE)." >&2
}

_seed_git_host_config

echo "$PROG: install complete (installer=$INSTALLER)." >&2
exit "$EXIT_OK"
