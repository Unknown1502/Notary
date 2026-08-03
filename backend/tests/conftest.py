"""Make the test suite runnable from any working directory.

Without this, `pytest backend/tests` from the repo root fails with
ModuleNotFoundError while `cd backend && pytest` succeeds — because the second
form happens to put the package directory on `sys.path` and the first does not.

CI installs the package (`pip install -e ./backend`), so it would pass there
either way. That divergence is exactly the problem: a suite that only runs from
one directory sends people chasing an import error that says nothing about the
real cause.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
