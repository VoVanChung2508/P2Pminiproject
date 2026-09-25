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
from tkinter import messagebox, ttk


ROOT = Path(__file__).resolve().parent
HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "18080"))
TCP_PORT = int(os.environ.get("MONITOR_TCP_PORT", "18888"))


class ServerManagerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Network Monitor - Server Manager")
        self.root.geometry("780x480")
        self.process: subprocess.Popen[str] | None = None
        self.polling = True

        self.status_var = tk.StringVar(value="STOPPED")
        self.summary_var = tk.StringVar(value="No monitoring server process")
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(1000, self.refresh)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=14)
        top.pack(fill="x")
        ttk.Label(top, text="NETWORK MONITOR", font=("Arial", 18, "bold")).pack(side="left")
        ttk.Label(top, textvariable=self.status_var, foreground="#16803c").pack(side="right")

        controls = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start Server", command=self.start_server)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="Stop Server", command=self.stop_server, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Open Dashboard", command=self.open_dashboard).pack(side="left")

        self.table = ttk.Treeview(
            self.root,
            columns=("client", "ip", "cpu", "ram", "disk", "status"),
            show="headings",
        )
        for column, title, width in (
            ("client", "Client", 140),
            ("ip", "IP", 140),
            ("cpu", "CPU", 90),
            ("ram", "RAM", 90),
            ("disk", "Disk", 90),
            ("status", "Status", 110),
        ):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, anchor="center")
        self.table.pack(fill="both", expand=True, padx=14, pady=4)

        ttk.Label(self.root, textvariable=self.summary_var, padding=14).pack(anchor="w")

        log_frame = ttk.LabelFrame(self.root, text="Server log", padding=8)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box = tk.Text(log_frame, height=6, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

    def write_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start_server(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        environment = os.environ.copy()
        environment["MONITOR_HTTP_PORT"] = str(HTTP_PORT)
        environment["MONITOR_TCP_PORT"] = str(TCP_PORT)
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "Main.py")],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self.read_process_log, daemon=True).start()
        self.write_log("Starting monitoring server...")
        self.update_controls(True)

    def read_process_log(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.root.after(0, self.write_log, line.rstrip())
        return_code = process.wait()
        if return_code != 0:
            self.root.after(
                0,
                self.write_log,
                f"Server stopped with exit code {return_code}. Check the port settings.",
            )

    def stop_server(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.update_controls(False)
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.write_log("Monitoring server stopped.")
        self.update_controls(False)

    def update_controls(self, running: bool) -> None:
        self.status_var.set("RUNNING" if running else "STOPPED")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def open_dashboard(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}")

    def refresh(self) -> None:
        if not self.polling:
            return
        process_running = self.process is not None and self.process.poll() is None
        if not process_running:
            self.update_controls(False)
        else:
            self.update_controls(True)
            threading.Thread(target=self.fetch_clients, daemon=True).start()
        self.root.after(3000, self.refresh)

    def fetch_clients(self) -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/clients", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.root.after(0, self.render_clients, payload.get("clients", []))
        except (urllib.error.URLError, TimeoutError, OSError):
            self.root.after(0, self.render_clients, [])

    def render_clients(self, clients: list[dict[str, object]]) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        online = 0
        for client in clients:
            status = str(client.get("status", "OFFLINE"))
            online += status == "ONLINE"
            self.table.insert(
                "",
                "end",
                values=(
                    client.get("name", ""),
                    client.get("ip", ""),
                    f"{client.get('cpu', 0)}%",
                    f"{client.get('ram', 0)}%",
                    f"{client.get('disk', 0)}%",
                    status,
                ),
            )
        self.summary_var.set(f"Clients: {len(clients)} | Online: {online} | TCP: {TCP_PORT} | HTTP: {HTTP_PORT}")

    def close(self) -> None:
        self.polling = False
        if self.process is not None and self.process.poll() is None:
            if messagebox.askyesno("Exit", "Stop the monitoring server and exit?"):
                self.stop_server()
            else:
                return
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    ServerManagerGUI(window)
    window.mainloop()
