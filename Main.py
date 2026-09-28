from __future__ import annotations

import tkinter as tk

from server.server_gui import ServerManagerGUI


def main() -> None:
    """Open the server manager; the managed server owns its MySQL connection."""
    window = tk.Tk()
    ServerManagerGUI(window)
    window.mainloop()


if __name__ == "__main__":
    main()
