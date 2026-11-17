"""repo_checks.exits — the exit codes of ``clagentic-loadout lint`` and ``build``.

The range 120-125 is these verbs' own. A check command's own exit code is
recorded in the run and never returned, so none of these can be mistaken for it.

  0    ok (run: every check passed; report: the report was printed)
  1    usage error (including ``--only`` naming a check the profile does not declare)
  120  run: a check failed, timed out or could not start; later checks did not run
  121  run: the profile is invalid, or its lint or build block is malformed
  122  run: the profile, or its lint or build block, declares nothing to run
  123  run: the run could not be recorded under the reports root
  124  report: the run id is malformed, unknown, or belongs to the other verb
  125  report: the recorded run exists but cannot be read
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CHECK_FAILED = 120
EXIT_PROFILE_INVALID = 121
EXIT_NOT_DECLARED = 122
EXIT_STORE_WRITE = 123
EXIT_RUN_NOT_FOUND = 124
EXIT_REPORT_UNREADABLE = 125
