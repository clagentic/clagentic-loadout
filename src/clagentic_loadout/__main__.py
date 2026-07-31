"""Allow ``python3 -m clagentic_loadout`` as an equivalent to the console script."""

from __future__ import annotations

import sys

from clagentic_loadout.cli import main

if __name__ == "__main__":
    sys.exit(main())
