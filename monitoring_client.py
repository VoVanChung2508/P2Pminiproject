"""Root launcher for Client Agent (GUI & CLI)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.monitoring_client import main

if __name__ == "__main__":
    main()
