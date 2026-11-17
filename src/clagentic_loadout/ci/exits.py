"""ci.exits -- the exit codes of ``clagentic-loadout ci``.

The range 100-109 is this verb's own. No child-process code is passed through.

  0    ok: the host accepted the operation
  1    usage error
  100  no credential could be resolved for the caller's role
  101  --caller does not match the attested invoking identity
  102  the platform offers no such operation (the message names the platform and
       the operation); no request was made
  103  the host answered 404: no such run in that repository
  104  the host answered 403: the caller's credential may not do this
  105  the host answered 409 or 422: the run is not in a state that allows the
       operation (a stuck run is cancelled first, then re-run)
  106  any other host answer, or none (network failure, redirect, 5xx, an
       unexpected success status)
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 100
EXIT_CALLER_INVOKER_MISMATCH = 101
EXIT_NOT_SUPPORTED = 102
EXIT_RUN_NOT_FOUND = 103
EXIT_FORBIDDEN = 104
EXIT_STATE_REFUSED = 105
EXIT_HOST_ERROR = 106
