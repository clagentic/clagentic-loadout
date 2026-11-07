"""identity.verb -- `loadout-identity`: show or check the resolved identity.

  loadout-identity whoami
  loadout-identity verify TOKEN [--dir DIR | --peer-id ID] [--now SECONDS]

`whoami` resolves the identity through the provider seam and prints the `Actor`
as one JSON object (provider and self_declared included). `verify` checks one
token against the trusted issuers the configured assertion provider names and
prints the verified claims; local mode checks the `dir` binding against --dir
(default: CLAGENTIC_IDENTITY_DIR), peer mode checks `aud` against --peer-id.

The verb mints no credential and binds no caller. Neither command falls back to
another identity source.

Exit codes (reserved range 90-111 for refusals, one per failure kind):

  0   ok
  1   usage error
  90  identity_dir_missing       91  identity_dir_unsafe
  92  identity_file_missing      93  identity_file_symlink
  94  malformed                 95  alg_not_allowed
  96  issuer_untrusted          97  key_not_found
  98  signature_invalid         99  contract_version_unsupported
  100 expired                   101 not_yet_valid
  102 ttl_exceeded              103 claims_invalid
  104 audience_unexpected       105 audience_mismatch
  106 dir_mismatch              107 provider_unset
  108 config_invalid            109 jwks_unavailable
  110 provider_module_invalid   111 attestation_refused
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from clagentic_loadout._version import get_version
from clagentic_loadout.identity import errors
from clagentic_loadout.identity.config import load_identity_config
from clagentic_loadout.identity.delivery import IDENTITY_DIR_ENV
from clagentic_loadout.identity.jwks_source import load_jwks
from clagentic_loadout.identity.seam import resolve
from clagentic_loadout.identity.verifier import MODE_LOCAL, MODE_PEER, verify

EXIT_OK = 0
EXIT_USAGE = 1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-identity",
        description="Show the identity the configured provider resolves, or verify one assertion token.",
    )
    parser.add_argument("--version", action="version", version=f"loadout-identity {get_version()}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="Resolve the identity and print it as JSON.")
    check = sub.add_parser("verify", help="Verify one assertion token and print its claims.")
    check.add_argument("token", help="Compact JWS to verify.")
    where = check.add_mutually_exclusive_group()
    where.add_argument("--dir", help="Directory the token is deemed read from (local mode).")
    where.add_argument("--peer-id", help="Verify as this peer, in peer mode.")
    check.add_argument("--now", type=int, help="Verification time as a NumericDate (default: now).")
    return parser


def _verify_command(args: argparse.Namespace, env: Mapping[str, str], config_root) -> dict:
    cfg = load_identity_config(config_root=config_root)
    _kind, issuers = cfg.assertion_issuers()
    locators = {i.iss: i.locator for i in issuers}
    mode = MODE_PEER if args.peer_id else MODE_LOCAL
    observed = args.dir or (env.get(IDENTITY_DIR_ENV) or "").strip() or None
    if mode == MODE_LOCAL and observed is None:
        raise errors.IdentityError(
            errors.IDENTITY_DIR_MISSING, f"pass --dir or set {IDENTITY_DIR_ENV} for the dir binding"
        )
    return verify(
        args.token,
        trusted_issuers=tuple(locators),
        jwks_loader=lambda iss: load_jwks(locators[iss]),
        mode=mode,
        observed_dir=observed,
        peer_id=args.peer_id,
        now=args.now,
    )


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config_root: str | Path | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code. *env* and *config_root* are
    for tests and embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    active_env = os.environ if env is None else env
    try:
        if args.command == "whoami":
            out = resolve(env=active_env, config_root=config_root).to_json_dict()
        else:
            out = _verify_command(args, active_env, config_root)
    except errors.IdentityError as exc:
        print(f"loadout-identity: {exc}", file=sys.stderr)
        return errors.EXIT_CODES[exc.code]
    print(json.dumps(out, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
