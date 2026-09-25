from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from server.server_main import ServerMain


ROOT = Path(__file__).resolve().parent
MONITOR_HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "18080"))
MONITOR_TCP_PORT = int(os.environ.get("MONITOR_TCP_PORT", "18888"))


class UnifiedServerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("P2P and Network Monitoring Server")
        self.root.geometry("1100x720")
        self.monitor_process: subprocess.Popen[str] | None = None
        self.monitor_polling = True

        self.monitor_status = tk.StringVar(value="STOPPED")
        self.monitor_summary = tk.StringVar(value="Monitoring server is stopped")
        self.build_monitor_tab()
        self.build_p2p_tab()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(1000, self.refresh_monitor)

    def build_monitor_tab(self) -> None:
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True)
        monitor_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(monitor_tab, text="Network Monitoring")

        header = ttk.Frame(monitor_tab)
        header.pack(fill="x")
        ttk.Label(
            header, text="NETWORK MONITOR", font=("Arial", 18, "bold")
        ).pack(side="left")
        ttk.Label(header, textvariable=self.monitor_status).pack(side="right")

        controls = ttk.Frame(monitor_tab, padding=(0, 12))
        controls.pack(fill="x")
        self.monitor_start = ttk.Button(
            controls, text="Start Monitoring", command=self.start_monitor
        )
        self.monitor_start.pack(side="left", padx=(0, 8))
        self.monitor_stop = ttk.Button(
            controls, text="Stop Monitoring", command=self.stop_monitor, state="disabled"
        )
        self.monitor_stop.pack(side="left", padx=(0, 8))
        ttk.Button(
            controls, text="Open Web Dashboard", command=self.open_dashboard
        ).pack(side="left")

        self.monitor_table = ttk.Treeview(
            monitor_tab,
            columns=("client", "ip", "cpu", "ram", "disk", "status"),
            show="headings",
        )
        for column, title, width in (
            ("client", "Client", 150),
            ("ip", "IP", 150),
            ("cpu", "CPU", 100),
            ("ram", "RAM", 100),
            ("disk", "Disk", 100),
            ("status", "Status", 120),
        ):
            self.monitor_table.heading(column, text=title)
            self.monitor_table.column(column, width=width, anchor="center")
        self.monitor_table.pack(fill="both", expand=True)
        ttk.Label(monitor_tab, textvariable=self.monitor_summary).pack(anchor="w", pady=8)

        log_frame = ttk.LabelFrame(monitor_tab, text="Monitoring log", padding=6)
        log_frame.pack(fill="x")
        self.monitor_log = tk.Text(log_frame, height=6, state="disabled")
        self.monitor_log.pack(fill="x")

    def build_p2p_tab(self) -> None:
        p2p_tab = ttk.Frame(self.tabs)
        self.tabs.add(p2p_tab, text="P2P Directory")
        self.p2p_server = ServerMain(root=self.root, parent=p2p_tab)

    def write_monitor_log(self, message: str) -> None:
        self.monitor_log.configure(state="normal")
        self.monitor_log.insert("end", message + "\n")
        self.monitor_log.see("end")
        self.monitor_log.configure(state="disabled")

    def start_monitor(self) -> None:
        if self.monitor_process and self.monitor_process.poll() is None:
            return
        environment = os.environ.copy()
        environment["MONITOR_HTTP_PORT"] = str(MONITOR_HTTP_PORT)
        environment["MONITOR_TCP_PORT"] = str(MONITOR_TCP_PORT)
        self.monitor_process = subprocess.Popen(
            [sys.executable, str(ROOT / "Main.py")],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self.read_monitor_log, daemon=True).start()
        self.write_monitor_log("Starting monitoring server...")
        self.update_monitor_controls(True)

    def read_monitor_log(self) -> None:
        process = self.monitor_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.root.after(0, self.write_monitor_log, line.rstrip())
        exit_code = process.wait()
        if exit_code:
            self.root.after(0, self.write_monitor_log, f"Monitoring server exited: {exit_code}")

    def stop_monitor(self) -> None:
        if self.monitor_process and self.monitor_process.poll() is None:
            self.monitor_process.terminate()
            try:
                self.monitor_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.monitor_process.kill()
        self.update_monitor_controls(False)
        self.write_monitor_log("Monitoring server stopped.")

    def update_monitor_controls(self, running: bool) -> None:
        self.monitor_status.set("RUNNING" if running else "STOPPED")
        self.monitor_start.configure(state="disabled" if running else "normal")
        self.monitor_stop.configure(state="normal" if running else "disabled")

    def open_dashboard(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{MONITOR_HTTP_PORT}")

    def refresh_monitor(self) -> None:
        if not self.monitor_polling:
            return
        running = self.monitor_process is not None and self.monitor_process.poll() is None
        self.update_monitor_controls(running)
        if running:
            threading.Thread(target=self.fetch_monitor_clients, daemon=True).start()
        self.root.after(3000, self.refresh_monitor)

    def fetch_monitor_clients(self) -> None:
        try:
            url = f"http://127.0.0.1:{MONITOR_HTTP_PORT}/api/clients"
            with urllib.request.urlopen(url, timeout=1) as response:
                clients = json.loads(response.read().decode("utf-8")).get("clients", [])
        except (urllib.error.URLError, TimeoutError, OSError):
            clients = []
        self.root.after(0, self.render_monitor_clients, clients)

    def render_monitor_clients(self, clients: list[dict[str, object]]) -> None:
        for item in self.monitor_table.get_children():
            self.monitor_table.delete(item)
        online = 0
        for client in clients:
            status = str(client.get("status", "OFFLINE"))
            online += status == "ONLINE"
            self.monitor_table.insert(
                "", "end",
                values=(client.get("name", ""), client.get("ip", ""),
                        f"{client.get('cpu', 0)}%", f"{client.get('ram', 0)}%",
                        f"{client.get('disk', 0)}%", status),
            )
        self.monitor_summary.set(
            f"Clients: {len(clients)} | Online: {online} | "
            f"TCP: {MONITOR_TCP_PORT} | HTTP: {MONITOR_HTTP_PORT}"
        )

    def close(self) -> None:
        self.monitor_polling = False
        self.stop_monitor()
        self.p2p_server.on_close()


if __name__ == "__main__":
    window = tk.Tk()
    UnifiedServerGUI(window)
    window.mainloop()


if __name__ == "__main__":
    app = ServerMain()
    app.run()
