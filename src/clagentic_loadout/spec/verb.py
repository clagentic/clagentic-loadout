"""spec.verb -- `loadout-spec`: create, seal and read the standard spec.

  loadout-spec new  --role ROLE [--repo-path DIR] [--ref REV] [--supersedes HASH] [--out FILE]
  loadout-spec seal --file FILE [--repo-path DIR] [--head SHA]
  loadout-spec show HASH [--chain]

`new` writes a draft for ROLE: the sections the repo profile can fill (the class
rules, the declared test commands and the profile completeness items) are filled
from it, and every other section is present and empty. The draft is an ordinary file for the author to
edit. With --supersedes it starts from a sealed spec instead, which is the only
way to change one.

`seal` validates a finished draft, binds it to a branch head (--head, or the head
of the repository at --repo-path), stores it as a content-addressed artifact under
the specs root, and prints its hash with the copy to put in the pull request body.
The copy is the artifact's own bytes. A document that already carries a seal is
refused: no verb edits a sealed spec in place.

`show` prints the sealed spec stored under HASH, byte for byte, after recomputing
the hash and refusing a file that does not match its name. With --chain it prints
the hashes of the seals HASH supersedes instead.

The specs root is resolved as in spec.store: the CLAGENTIC_LOADOUT_SPECS_DIR
environment variable, the user-level ``specs.dir`` setting, then the default under
the user config root. A root inside the repository under review is refused.

The verb binds no caller and mints no credential; the role is the author's own
statement, kept in the sealed record. A sealed spec is protected from the verbs,
not from a process that can write the specs root as the same user.

Exit codes (the reserved range for this verb):

  0   ok
  1   usage error
  3   the specs root cannot be used or read
  50  no sealed spec has that hash
  51  a stored file is not the spec its name says (hash mismatch or invalid)
  52  the draft is not a complete, valid spec
  53  refused: the document is already sealed, or the supersession is not the
      same role's
  54  the repo profile is invalid
  55  a file or the repository could not be read or written
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.artifact_store import RootUnreadableError, StoreError
from clagentic_loadout.review.policy_source import PROFILE_INVALID, PROFILE_MISSING, PROFILE_PATH
from clagentic_loadout.review.profile_brief import (
    ProfileReadError,
    profile_brief,
    read_completeness,
    read_profile,
)
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.verdict_record.record import is_full_sha, utc_timestamp

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_STORE_UNUSABLE = 3
EXIT_NOT_FOUND = 50
EXIT_STORED_INVALID = 51
EXIT_DRAFT_INVALID = 52
EXIT_REFUSED = 53
EXIT_PROFILE_INVALID = 54
EXIT_IO_FAILED = 55

_MAX_DRAFT_BYTES = 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10


class SpecVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-spec",
        description=(
            "loadout-spec -- the standard spec. `new` writes a draft for a role with the "
            "sections the repo profile can fill already filled, `seal` stores a finished "
            "draft as a content-addressed artifact bound to a branch head and prints its "
            "hash and the pull request body copy, and `show` prints a sealed spec by hash "
            "after verifying it. A sealed spec is never edited: a change is a new draft "
            "that supersedes it, sealed again."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-spec new --role some-role --repo-path . --out spec.json\n"
            "  loadout-spec seal --file spec.json --repo-path .\n"
            "  loadout-spec show <64-character spec hash>\n"
            "  loadout-spec new --role some-role --supersedes <spec hash> --out spec-2.json\n"
            "\n"
            "Exit codes: 0 ok, 1 usage, 3 specs root unusable, 50 no such spec, 51 stored "
            "spec invalid, 52 draft invalid, 53 refused, 54 repo profile invalid, 55 file or "
            "repository unreadable."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-spec {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser(
        "new",
        help="Write a draft spec for a role.",
        description="Write a draft spec as JSON. The class rules, the declared test "
        "commands and the profile completeness items are filled from the repo profile; "
        "every other section is present and empty.",
    )
    new.add_argument("--role", required=True, help="The role authoring the spec, an opaque key.")
    new.add_argument(
        "--repo-path", default=".", help="Repository whose profile fills the draft (default: the current directory)."
    )
    new.add_argument(
        "--ref",
        default=None,
        help="Read the repo profile at this revision instead of the working tree.",
    )
    new.add_argument(
        "--supersedes",
        default=None,
        metavar="HASH",
        help="Start from this sealed spec (same role) and name it in the draft's supersedes.",
    )
    new.add_argument(
        "--out",
        default=None,
        help="Write the draft to this new file instead of standard output. An existing file is never replaced.",
    )

    seal = sub.add_parser(
        "seal",
        help="Seal a finished draft.",
        description="Validate a draft, bind it to a branch head, store it as an artifact, "
        "and print its hash and the pull request body copy as one JSON object.",
    )
    seal.add_argument("--file", required=True, help="The finished draft (JSON).")
    seal.add_argument(
        "--repo-path",
        default=".",
        help="The repository under review: the default source of --head, and a location the "
        "specs root may not be inside (default: the current directory).",
    )
    seal.add_argument(
        "--head",
        default=None,
        help="Full commit sha to bind the spec to (default: the head of --repo-path).",
    )

    show = sub.add_parser(
        "show",
        help="Print a sealed spec by hash.",
        description="Print the sealed spec stored under HASH, byte for byte, after verifying it.",
    )
    show.add_argument("spec_hash", metavar="HASH", help="The spec hash, as printed by seal.")
    show.add_argument(
        "--chain",
        action="store_true",
        help="Print the hashes of the seals HASH supersedes, newest first, as JSON.",
    )
    show.add_argument(
        "--repo-path",
        default=None,
        help="A repository the specs root may not be inside.",
    )
    return parser


def _git_output(repo_path: str, *git_args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", repo_path, *git_args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _untrusted_roots(repo_path: str | None) -> tuple[Path, ...]:
    """The repository the specs root must stay out of: the top level of the
    repository at *repo_path*, when it is one."""
    if repo_path is None:
        return ()
    toplevel = _git_output(repo_path, "rev-parse", "--show-toplevel")
    return (Path(toplevel),) if toplevel else ()


def _store_error_code(exc: StoreError) -> int:
    if isinstance(exc, spec_store.SpecNotFoundError):
        return EXIT_NOT_FOUND
    if isinstance(exc, spec_store.SpecInvalidError):
        return EXIT_STORED_INVALID
    return EXIT_STORE_UNUSABLE


def _load(root: Path, spec_hash: str, untrusted: tuple[Path, ...]) -> spec_store.StoredSpec:
    if not model.is_spec_hash(spec_hash):
        raise SpecVerbError(
            f"{spec_hash!r} is not a spec hash (expected 64 lowercase hex digits)", EXIT_USAGE
        )
    try:
        return spec_store.load_sealed(root, spec_hash, untrusted_roots=untrusted)
    except StoreError as exc:
        raise SpecVerbError(str(exc), _store_error_code(exc)) from exc


def _new(args: argparse.Namespace, root: Path, now: dt.datetime | None = None) -> int:
    try:
        draft = model.new_draft(args.role)
    except model.SpecFormatError as exc:
        raise SpecVerbError(str(exc), EXIT_USAGE) from exc
    if args.supersedes is not None:
        prior = _load(root, args.supersedes, _untrusted_roots(args.repo_path))
        if prior.doc["role"] != args.role:
            raise SpecVerbError(
                f"the spec {args.supersedes} was sealed by role {prior.doc['role']!r}; only that "
                f"role supersedes it, and --role is {args.role!r}",
                EXIT_REFUSED,
            )
        draft = model.draft_from_sealed(prior.doc, prior.spec_hash)
    try:
        profile, state, problem = read_profile(args.repo_path, args.ref)
    except ProfileReadError as exc:
        raise SpecVerbError(str(exc), EXIT_IO_FAILED) from exc
    if state == PROFILE_INVALID:
        raise SpecVerbError(
            f"the repo profile {PROFILE_PATH} in {args.repo_path!r} is invalid: {problem}",
            EXIT_PROFILE_INVALID,
        )
    if profile is not None:
        draft = model.apply_brief(draft, profile_brief(profile), absent_note="")
    else:
        reason = "no repo profile" if state == PROFILE_MISSING else "the repo profile was not read"
        draft = model.apply_brief(
            draft, None, absent_note=f"{reason} at {PROFILE_PATH} in {args.repo_path!r}"
        )
    try:
        items = read_completeness(args.repo_path, args.ref, now=now)
    except ProfileReadError as exc:
        raise SpecVerbError(str(exc), EXIT_IO_FAILED) from exc
    draft = model.apply_completeness(draft, [item.render() for item in items])
    text = json.dumps(draft, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
        return EXIT_OK
    try:
        with open(args.out, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise SpecVerbError(
            f"{args.out!r} already exists; this verb never replaces a file", EXIT_IO_FAILED
        ) from exc
    except OSError as exc:
        raise SpecVerbError(f"cannot write {args.out!r}: {exc}", EXIT_IO_FAILED) from exc
    print(json.dumps({"result": "written", "path": args.out}))
    return EXIT_OK


def _read_draft(path: str) -> object:
    try:
        size = Path(path).stat().st_size
        if size > _MAX_DRAFT_BYTES:
            raise SpecVerbError(
                f"{path!r} is {size} bytes, larger than the {_MAX_DRAFT_BYTES} byte limit",
                EXIT_IO_FAILED,
            )
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecVerbError(f"cannot read the draft {path!r}: {exc}", EXIT_IO_FAILED) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecVerbError(f"the draft {path!r} is not valid JSON: {exc}", EXIT_DRAFT_INVALID) from exc


def _seal(args: argparse.Namespace, root: Path) -> int:
    draft = _read_draft(args.file)
    head = args.head if args.head is not None else _git_output(args.repo_path, "rev-parse", "HEAD")
    if head is None:
        raise SpecVerbError(
            f"cannot read the head of the repository at {args.repo_path!r}; pass --head", EXIT_IO_FAILED
        )
    if not is_full_sha(head):
        raise SpecVerbError(
            f"the head to bind is {head!r}, not a full commit sha (40 or 64 lowercase hex digits)",
            EXIT_USAGE,
        )
    untrusted = _untrusted_roots(args.repo_path)
    try:
        sealed = model.seal(draft, head_sha=head, sealed_at=utc_timestamp())
    except model.AlreadySealedError as exc:
        raise SpecVerbError(str(exc), EXIT_REFUSED) from exc
    except model.SpecFormatError as exc:
        raise SpecVerbError(f"the draft {args.file!r} is not a valid spec: {exc}", EXIT_DRAFT_INVALID) from exc
    prior_hash = sealed.doc["supersedes"]
    if prior_hash is not None:
        prior = _load(root, prior_hash, untrusted)
        if prior.doc["role"] != sealed.doc["role"]:
            raise SpecVerbError(
                f"the spec {prior_hash} was sealed by role {prior.doc['role']!r}; only that role "
                f"supersedes it, and this draft is for role {sealed.doc['role']!r}",
                EXIT_REFUSED,
            )
    try:
        path = spec_store.write_sealed(root, sealed, untrusted_roots=untrusted)
    except StoreError as exc:
        raise SpecVerbError(str(exc), _store_error_code(exc)) from exc
    print(
        json.dumps(
            {
                "spec_hash": sealed.spec_hash,
                "head_sha": head,
                "role": sealed.doc["role"],
                "sealed_at": sealed.doc["sealed_at"],
                "supersedes": prior_hash,
                "artifact": str(path),
                "pr_body_copy": sealed.data.decode("ascii"),
            }
        )
    )
    return EXIT_OK


def _show(args: argparse.Namespace, root: Path) -> int:
    untrusted = _untrusted_roots(args.repo_path)
    if args.chain:
        if not model.is_spec_hash(args.spec_hash):
            raise SpecVerbError(
                f"{args.spec_hash!r} is not a spec hash (expected 64 lowercase hex digits)",
                EXIT_USAGE,
            )
        try:
            links = spec_store.chain(root, args.spec_hash, untrusted_roots=untrusted)
        except StoreError as exc:
            raise SpecVerbError(str(exc), _store_error_code(exc)) from exc
        print(json.dumps({"spec_hash": args.spec_hash, "chain": [link.spec_hash for link in links]}))
        return EXIT_OK
    stored = _load(root, args.spec_hash, untrusted)
    sys.stdout.write(stored.data.decode("ascii"))
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    config_root: str | Path | None = None,
    now: dt.datetime | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code. *config_root* and *now*
    (the clock the profile completeness is measured against) are for tests and
    embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    root = spec_store.specs_root(config_root=config_root)
    try:
        if args.command == "new":
            return _new(args, root, now)
        if args.command == "seal":
            return _seal(args, root)
        return _show(args, root)
    except SpecVerbError as exc:
        print(f"loadout-spec: {exc}", file=sys.stderr)
        return exc.code
    except RootUnreadableError as exc:
        print(f"loadout-spec: {exc}", file=sys.stderr)
        return EXIT_STORE_UNUSABLE


if __name__ == "__main__":
    sys.exit(main())
