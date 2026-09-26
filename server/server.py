from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

# Add project root to sys.path so imports work regardless of execution location
SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

try:
    from server.server_gui import ServerManagerGUI
except (ImportError, ModuleNotFoundError):
    from server_gui import ServerManagerGUI


def main() -> None:
    window = tk.Tk()
    ServerManagerGUI(window)
    window.mainloop()


if __name__ == "__main__":
    main()
