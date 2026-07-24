"""Pytest config for the SciKick server test suite.

The server modules use flat imports (``from config import ...``,
``from file_processor import ...``), so ``server/`` must be on ``sys.path``
for tests to import them. This conftest adds it.
"""

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
