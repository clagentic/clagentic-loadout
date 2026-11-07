"""identity.errors -- the typed refusal every identity path raises.

An identity is verified, explicitly declared, or refused; a refusal is always an
`IdentityError` carrying one stable `code`. The verification codes are the failure
codes of the published identity contract (same spelling), so a refusal here can be
compared with the conformance vectors directly. The remaining codes are this
package's own: configuration and provider-seam failures.
"""

from __future__ import annotations

# Contract failure codes (verification and local delivery).
IDENTITY_DIR_MISSING = "identity_dir_missing"
IDENTITY_DIR_UNSAFE = "identity_dir_unsafe"
IDENTITY_FILE_MISSING = "identity_file_missing"
IDENTITY_FILE_SYMLINK = "identity_file_symlink"
MALFORMED = "malformed"
ALG_NOT_ALLOWED = "alg_not_allowed"
ISSUER_UNTRUSTED = "issuer_untrusted"
KEY_NOT_FOUND = "key_not_found"
SIGNATURE_INVALID = "signature_invalid"
CONTRACT_VERSION_UNSUPPORTED = "contract_version_unsupported"
EXPIRED = "expired"
NOT_YET_VALID = "not_yet_valid"
TTL_EXCEEDED = "ttl_exceeded"
CLAIMS_INVALID = "claims_invalid"
AUDIENCE_UNEXPECTED = "audience_unexpected"
AUDIENCE_MISMATCH = "audience_mismatch"
DIR_MISMATCH = "dir_mismatch"

# Seam codes.
PROVIDER_UNSET = "provider_unset"
CONFIG_INVALID = "config_invalid"
JWKS_UNAVAILABLE = "jwks_unavailable"
PROVIDER_MODULE_INVALID = "provider_module_invalid"
ATTESTATION_REFUSED = "attestation_refused"

#: code -> process exit code for `loadout-identity`. One distinct code per failure
#: kind, all inside the verb's reserved range 90-111.
EXIT_CODES: dict[str, int] = {
    IDENTITY_DIR_MISSING: 90,
    IDENTITY_DIR_UNSAFE: 91,
    IDENTITY_FILE_MISSING: 92,
    IDENTITY_FILE_SYMLINK: 93,
    MALFORMED: 94,
    ALG_NOT_ALLOWED: 95,
    ISSUER_UNTRUSTED: 96,
    KEY_NOT_FOUND: 97,
    SIGNATURE_INVALID: 98,
    CONTRACT_VERSION_UNSUPPORTED: 99,
    EXPIRED: 100,
    NOT_YET_VALID: 101,
    TTL_EXCEEDED: 102,
    CLAIMS_INVALID: 103,
    AUDIENCE_UNEXPECTED: 104,
    AUDIENCE_MISMATCH: 105,
    DIR_MISMATCH: 106,
    PROVIDER_UNSET: 107,
    CONFIG_INVALID: 108,
    JWKS_UNAVAILABLE: 109,
    PROVIDER_MODULE_INVALID: 110,
    ATTESTATION_REFUSED: 111,
}


class IdentityError(Exception):
    """A refusal to return an identity. `code` is one key of `EXIT_CODES`."""

    def __init__(self, code: str, message: str) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown identity error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
