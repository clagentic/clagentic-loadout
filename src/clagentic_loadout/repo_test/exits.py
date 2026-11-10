"""repo_test.exits — the exit codes of ``clagentic-loadout test``.

The range 70-79 is this verb's own. A test command's own exit code is recorded
in the run and never returned, so none of these can be mistaken for it.

  0   ok (run: every test passed; report: the answer was printed)
  1   usage error
  70  run: the tests failed (a failing test, or a non-zero command exit)
  71  run: the repository has no valid profile
  72  run: the profile does not declare what this run needs (names the key)
  73  run: the command could not run, or left no readable JUnit report
  74  run: the run could not be recorded under the reports root
  75  report: the reports root is missing, or holds no matching run
  76  report: the run id is malformed, or no such run exists
  77  report: no run matches --sha (names what exists), or the prefix is ambiguous
  78  refused: a repository mismatch (run --expect-repo/--expect-head, report
      --repo on an explicit run id, `latest` with no repository to scope to
      and several on record, --diff across repositories), --diff across unlike
      tiers, or an unrunnable run, which has no results to list or compare.
      Reused rather than extended: every one of these is a refusal to act
      on a run or tree that is not the one the caller named, and the range 70-79
      has no free code while 80 and up belong to other verbs.
  79  report: the run is still running (--wait timed out, or --failing-ids/--diff on
      a running run). Also the installer helper's (repo_test.permission) code for a
      settings file that could not be read or written; that helper is a separate
      process that is never a `clagentic-loadout test` invocation, so the two
      meanings cannot be returned to the same caller.
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TESTS_FAILED = 70
EXIT_PROFILE = 71
EXIT_NOT_DECLARED = 72
EXIT_UNRUNNABLE = 73
EXIT_STORE_WRITE = 74
EXIT_NO_RUNS = 75
EXIT_BAD_RUN_ID = 76
EXIT_SHA_NO_MATCH = 77
EXIT_DIFF_REFUSED = 78
EXIT_REPO_MISMATCH = EXIT_DIFF_REFUSED
EXIT_SETTINGS = 79
EXIT_STILL_RUNNING = 79
