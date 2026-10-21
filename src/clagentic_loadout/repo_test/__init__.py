"""repo_test — run a repository's declared tests and read the results by run id.

The commands a repository runs come from the ``test`` block of its repo profile
(see ``repo_test.spec``); the results of each run are kept under a reports root
owned by loadout (see ``repo_test.store``) and are read back through
``clagentic-loadout test report``, which takes run ids and never filesystem
paths.

This package marker imports nothing on purpose: ``review.repo_profile`` imports
``repo_test.spec`` to parse the profile's ``test`` block, and every other module
here imports the review package, so any import here would be a cycle.
"""
