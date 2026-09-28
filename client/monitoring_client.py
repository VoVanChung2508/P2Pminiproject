from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

try:
    import psutil
except ImportError:
    psutil = None

# Ensure project root is in sys.path
_current_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_current_dir, ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    from client.http_client import HTTPClient
    from client.tcp_client import TCPClient
except ImportError:
    try:
        from http_client import HTTPClient
        from tcp_client import TCPClient
    except ImportError:
        HTTPClient = None
        TCPClient = None

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    TKINTER_AVAILABLE = True
except ImportError:
    tk = None
    messagebox = None
    ttk = None
    TKINTER_AVAILABLE = False

DEFAULT_HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "8081"))
DEFAULT_TCP_PORT = int(os.environ.get("MONITOR_TCP_PORT", "8888"))


def was_disconnected_by_server(response: Dict[str, Any]) -> bool:
    raw = str(response.get("raw", "")).strip().upper()
    message = str(response.get("message", "")).strip().upper()
    return raw == "ERROR|DISCONNECTED" or message == "DISCONNECTED"


# ============================================================================
# 1. CORE CLIENT ENGINE (NetworkMonitoringClient)
# ============================================================================

class NetworkMonitoringClient:
    """Core network monitoring client.
    
    Collects real-time OS resource metrics (CPU, RAM, Disk, Network) and communicates
    with the monitoring server via TCP protocol and REST APIs.
    """

    def __init__(
        self,
        name: str,
        host: str = "127.0.0.1",
        tcp_port: int = DEFAULT_TCP_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
    ):
        self.name = name
        self.host = host
        self.tcp_port = tcp_port
        self.http_port = http_port
        if TCPClient is not None:
            self.tcp_client = TCPClient(host=host, port=tcp_port, default_name=name)
        else:
            self.tcp_client = None
        if HTTPClient is not None:
            self.http_client = HTTPClient(f"http://{host}:{http_port}")
        else:
            self.http_client = None

        self._last_net_bytes: Optional[int] = None
        self._last_net_time: Optional[float] = None

    def register(self) -> Dict[str, Any]:
        """Register client with the monitoring TCP server."""
        if self.tcp_client:
            return self.tcp_client.register(self.name, host=self.host, port=self.tcp_port)
        return self._raw_tcp_send(f"REGISTER|{self.name}|{self.host}|{self.tcp_port}")

    def send_heartbeat(self) -> Dict[str, Any]:
        """Send HEARTBEAT message to maintain ONLINE status."""
        if self.tcp_client:
            return self.tcp_client.heartbeat(self.name)
        return self._raw_tcp_send(f"HEARTBEAT|{self.name}")

    def send_metrics(
        self, cpu: float = 0.0, ram: float = 0.0, disk: float = 0.0, network: float = 0.0
    ) -> Dict[str, Any]:
        """Send real-time resource metrics to the server."""
        if self.tcp_client:
            return self.tcp_client.send_metrics(
                self.name, cpu=cpu, ram=ram, disk=disk, network=network
            )
        msg = f"SYSTEM|{self.name}|cpu={cpu}|ram={ram}|disk={disk}|network={network}"
        return self._raw_tcp_send(msg)

    def collect_system_metrics(self) -> Dict[str, float]:
        """Collect actual hardware metrics using psutil."""
        if psutil is None:
            return {"cpu": 0.0, "ram": 0.0, "disk": 0.0, "network": 0.0}

        cpu = round(float(psutil.cpu_percent(interval=0.1)), 1)
        ram = round(float(psutil.virtual_memory().percent), 1)

        try:
            drive = os.path.abspath(os.sep)
            disk = round(float(psutil.disk_usage(drive).percent), 1)
        except Exception:
            disk = 0.0

        network = 0.0
        try:
            net_io = psutil.net_io_counters()
            current_bytes = net_io.bytes_sent + net_io.bytes_recv
            current_time = time.time()
            if self._last_net_bytes is not None and self._last_net_time is not None:
                elapsed = max(0.001, current_time - self._last_net_time)
                bytes_per_sec = (current_bytes - self._last_net_bytes) / elapsed
                # Scale network activity (e.g. 10MB/s reference bandwidth -> 100%)
                network = round(min(100.0, max(0.0, (bytes_per_sec / (10 * 1024 * 1024)) * 100.0)), 1)
            self._last_net_bytes = current_bytes
            self._last_net_time = current_time
        except Exception:
            network = 0.0

        return {"cpu": cpu, "ram": ram, "disk": disk, "network": network}

    def list_peers(self) -> Any:
        """Query REST API for list of connected clients."""
        if self.http_client:
            return self.http_client.list_clients()
        return {"status": "error", "message": "HTTPClient not available"}

    def health(self) -> Any:
        """Query REST API server health endpoint."""
        if self.http_client:
            return self.http_client.health()
        return {"status": "error", "message": "HTTPClient not available"}

    def ping(self) -> Dict[str, Any]:
        """Ping server via heartbeat."""
        return self.send_heartbeat()

    def disconnect(self) -> Dict[str, Any]:
        """Send LOGOUT message to notify server before exiting."""
        if self.tcp_client:
            return self.tcp_client.disconnect(self.name)
        return self._raw_tcp_send(f"LOGOUT|{self.name}")

    def _raw_tcp_send(self, message: str) -> Dict[str, Any]:
        """Fallback raw socket sender if tcp_client is unavailable."""
        try:
            with socket.create_connection((self.host, self.tcp_port), timeout=5) as sock:
                if message.startswith("REGISTER|"):
                    message += "|PROCESS_LIST_V1"
                sock.sendall((message + "\n").encode("utf-8"))
                data = sock.recv(4096)
                if not data:
                    return {"status": "error", "message": "No response"}
                raw = data.decode("utf-8", errors="ignore").strip()
                parts = raw.split("|", 1)
                return {
                    "status": "ok" if parts[0].upper() == "OK" else "error",
                    "raw": raw,
                    "message": parts[1] if len(parts) > 1 else raw,
                }
        except (OSError, socket.timeout) as exc:
            return {"status": "error", "message": str(exc)}


# Aliases for backward compatibility
MonitoringClient = NetworkMonitoringClient
P2PClient = NetworkMonitoringClient


# ============================================================================
# 2. DESKTOP GUI AGENT (ClientGUI)
# ============================================================================

class ClientGUI:
    """Desktop Graphical User Interface for Client Monitoring Agent."""

    def __init__(
        self,
        name: Optional[str] = None,
        host: str = "127.0.0.1",
        tcp_port: int = DEFAULT_TCP_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        interval: int = 3,
    ) -> None:
        if not TKINTER_AVAILABLE:
            raise RuntimeError("Tkinter is not available in the current Python environment.")

        self.root = tk.Tk()
        self.root.title("Network Monitoring System - Client Agent")
        self.root.geometry("640x620")
        self.root.minsize(550, 520)

        # State
        self.is_monitoring = False
        self.worker_thread: threading.Thread | None = None
        self.client: NetworkMonitoringClient | None = None

        # Variables
        default_name = name or (socket.gethostname() if socket.gethostname() else "Node-01")
        self.name_var = tk.StringVar(value=default_name)
        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(tcp_port))
        self.http_port_var = tk.StringVar(value=str(http_port))
        self.interval_var = tk.StringVar(value=str(interval))

        self.status_var = tk.StringVar(value="Trạng thái: CHƯA KẾT NỐI")
        self.cpu_var = tk.StringVar(value="0.0%")
        self.ram_var = tk.StringVar(value="0.0%")
        self.disk_var = tk.StringVar(value="0.0%")
        self.net_var = tk.StringVar(value="0.0%")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        # Header banner
        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="NETWORK MONITORING AGENT",
            font=("Segoe UI", 16, "bold"),
            foreground="#0284c7",
        ).pack(side="left")
        self.status_label = ttk.Label(
            header, textvariable=self.status_var, font=("Segoe UI", 10, "bold"), foreground="#dc2626"
        )
        self.status_label.pack(side="right")

        # Config Panel
        config_frame = ttk.LabelFrame(self.root, text="Cấu hình Kết nối & Thiết bị", padding=10)
        config_frame.pack(fill="x", padx=12, pady=(0, 8))

        row1 = ttk.Frame(config_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Tên Client:", width=12).pack(side="left")
        self.name_entry = ttk.Entry(row1, textvariable=self.name_var, width=20)
        self.name_entry.pack(side="left", padx=(0, 15))

        ttk.Label(row1, text="Server Host:", width=12).pack(side="left")
        self.host_entry = ttk.Entry(row1, textvariable=self.host_var, width=18)
        self.host_entry.pack(side="left")

        row2 = ttk.Frame(config_frame)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="TCP Port:", width=12).pack(side="left")
        self.port_entry = ttk.Entry(row2, textvariable=self.port_var, width=10)
        self.port_entry.pack(side="left", padx=(0, 15))

        ttk.Label(row2, text="HTTP Port:", width=10).pack(side="left")
        self.http_port_entry = ttk.Entry(row2, textvariable=self.http_port_var, width=10)
        self.http_port_entry.pack(side="left", padx=(0, 15))

        ttk.Label(row2, text="Chu kỳ (giây):", width=12).pack(side="left")
        self.interval_entry = ttk.Entry(row2, textvariable=self.interval_var, width=6)
        self.interval_entry.pack(side="left")

        # Live Metrics Panel
        metrics_frame = ttk.LabelFrame(self.root, text="Thông số Tài nguyên Hệ thống Thực tế", padding=10)
        metrics_frame.pack(fill="x", padx=12, pady=6)

        m_grid = ttk.Frame(metrics_frame)
        m_grid.pack(fill="x")
        m_grid.columnconfigure(1, weight=1)
        m_grid.columnconfigure(4, weight=1)

        # CPU
        ttk.Label(m_grid, text="CPU Usage:", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=4)
        self.cpu_bar = ttk.Progressbar(m_grid, maximum=100)
        self.cpu_bar.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(m_grid, textvariable=self.cpu_var, width=8).grid(row=0, column=2, sticky="w")

        # RAM
        ttk.Label(m_grid, text="RAM Usage:", font=("Segoe UI", 9, "bold")).grid(row=0, column=3, sticky="w", padx=(10, 0), pady=4)
        self.ram_bar = ttk.Progressbar(m_grid, maximum=100)
        self.ram_bar.grid(row=0, column=4, sticky="ew", padx=8, pady=4)
        ttk.Label(m_grid, textvariable=self.ram_var, width=8).grid(row=0, column=5, sticky="w")

        # Disk
        ttk.Label(m_grid, text="Disk Usage:", font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="w", pady=4)
        self.disk_bar = ttk.Progressbar(m_grid, maximum=100)
        self.disk_bar.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(m_grid, textvariable=self.disk_var, width=8).grid(row=1, column=2, sticky="w")

        # Network
        ttk.Label(m_grid, text="Network Load:", font=("Segoe UI", 9, "bold")).grid(row=1, column=3, sticky="w", padx=(10, 0), pady=4)
        self.net_bar = ttk.Progressbar(m_grid, maximum=100)
        self.net_bar.grid(row=1, column=4, sticky="ew", padx=8, pady=4)
        ttk.Label(m_grid, textvariable=self.net_var, width=8).grid(row=1, column=5, sticky="w")

        # Controls Panel
        btn_frame = ttk.Frame(self.root, padding=6)
        btn_frame.pack(fill="x", padx=12, pady=4)

        self.start_btn = ttk.Button(btn_frame, text="Bắt đầu Giám sát", command=self.start_monitoring)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(btn_frame, text="Dừng Giám sát", command=self.stop_monitoring, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))

        self.health_btn = ttk.Button(btn_frame, text="Kiểm tra Server Health", command=self.check_health)
        self.health_btn.pack(side="left", padx=(0, 8))

        ttk.Button(btn_frame, text="Xóa Log", command=self.clear_log).pack(side="right")

        # Event Log
        log_frame = ttk.LabelFrame(self.root, text="Nhật ký Hoạt động (Agent Log)", padding=8)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.log_text = tk.Text(log_frame, wrap="none", font=("Consolas", 9), state="disabled", bg="#0f172a", fg="#e2e8f0")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def append_log(self, msg: str) -> None:
        t = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{t}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.name_entry.configure(state=state)
        self.host_entry.configure(state=state)
        self.port_entry.configure(state=state)
        self.http_port_entry.configure(state=state)
        self.interval_entry.configure(state=state)

    def check_health(self) -> None:
        host = self.host_var.get().strip()
        http_port = int(self.http_port_var.get().strip() or str(DEFAULT_HTTP_PORT))
        client = NetworkMonitoringClient("probe", host=host, http_port=http_port)
        res = client.health()
        self.append_log(f"Kiểm tra Health http://{host}:{http_port} -> {res}")

    def start_monitoring(self) -> None:
        if self.is_monitoring:
            return

        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        try:
            tcp_port = int(self.port_var.get().strip())
            http_port = int(self.http_port_var.get().strip())
            interval = max(1, int(self.interval_var.get().strip()))
        except ValueError:
            messagebox.showerror("Lỗi", "Số cổng và chu kỳ phải là số nguyên dương hợp lệ.")
            return

        if not name:
            messagebox.showerror("Lỗi", "Tên Client không được để trống.")
            return

        self.client = NetworkMonitoringClient(name=name, host=host, tcp_port=tcp_port, http_port=http_port)

        # Send initial registration
        reg_res = self.client.register()
        if reg_res.get("status") != "ok":
            self.append_log(f"Đăng ký thất bại: {reg_res.get('message', 'Không kết nối được server')}")
            messagebox.showerror("Lỗi kết nối", f"Không thể đăng ký đến Server ({host}:{tcp_port}):\n{reg_res.get('message')}")
            return

        self.append_log(f"Đăng ký thành công client '{name}' tới Server {host}:{tcp_port}")
        self.is_monitoring = True
        self.set_inputs_enabled(False)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Trạng thái: ĐANG GIÁM SÁT (ONLINE)")
        self.status_label.configure(foreground="#16a34a")

        self.worker_thread = threading.Thread(target=self._monitoring_loop, args=(interval,), daemon=True)
        self.worker_thread.start()

    def _monitoring_loop(self, interval: int) -> None:
        while self.is_monitoring and self.client:
            try:
                metrics = self.client.collect_system_metrics()

                # Update UI meters on main thread
                self.root.after(0, self._update_metrics_ui, metrics)

                # Send metrics
                res_metric = self.client.send_metrics(**metrics)
                if was_disconnected_by_server(res_metric):
                    self.root.after(0, self._handle_server_disconnect)
                    break

                # Send heartbeat
                res_hb = self.client.send_heartbeat()
                if was_disconnected_by_server(res_hb):
                    self.root.after(0, self._handle_server_disconnect)
                    break

                self.root.after(
                    0,
                    self.append_log,
                    f"Gửi số liệu: CPU {metrics['cpu']}% | RAM {metrics['ram']}% | DISK {metrics['disk']}% | NET {metrics['network']}% -> {res_metric.get('status')}",
                )
            except Exception as exc:
                self.root.after(0, self.append_log, f"Lỗi trong chu kỳ gửi: {exc}")

            # Sleep in small increments for responsive stop
            for _ in range(interval * 2):
                if not self.is_monitoring:
                    break
                time.sleep(0.5)

    def _update_metrics_ui(self, m: dict[str, float]) -> None:
        cpu = m.get("cpu", 0.0)
        ram = m.get("ram", 0.0)
        disk = m.get("disk", 0.0)
        net = m.get("network", 0.0)

        self.cpu_bar["value"] = cpu
        self.cpu_var.set(f"{cpu}%")

        self.ram_bar["value"] = ram
        self.ram_var.set(f"{ram}%")

        self.disk_bar["value"] = disk
        self.disk_var.set(f"{disk}%")

        self.net_bar["value"] = net
        self.net_var.set(f"{net}%")

    def stop_monitoring(self) -> None:
        if not self.is_monitoring:
            return

        self.is_monitoring = False
        if self.client:
            res = self.client.disconnect()
            self.append_log(f"Đã gửi lệnh ngắt kết nối (LOGOUT): {res}")

        self.set_inputs_enabled(True)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Trạng thái: ĐÃ NGẮT KẾT NỐI (OFFLINE)")
        self.status_label.configure(foreground="#dc2626")

    def _handle_server_disconnect(self) -> None:
        if not self.is_monitoring:
            return
        self.is_monitoring = False
        self.append_log("Server đã chủ động ngắt client này. Hãy kết nối lại thủ công nếu cần.")
        self.set_inputs_enabled(True)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Trạng thái: BỊ SERVER NGẮT (OFFLINE)")
        self.status_label.configure(foreground="#dc2626")

    def on_close(self) -> None:
        if self.is_monitoring:
            self.stop_monitoring()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ============================================================================
# 3. CLI AGENT RUNNER (Headless / Background mode)
# ============================================================================

def run_cli(
    name: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_TCP_PORT,
    http_port: int = DEFAULT_HTTP_PORT,
    interval: int = 3,
) -> None:
    """Run monitoring agent in command-line mode without GUI."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Khởi động Client CLI: '{name}'")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Kết nối đến Monitoring Server tại {host}:{port}...")

    client = NetworkMonitoringClient(name=name, host=host, tcp_port=port, http_port=http_port)

    # Check REST health
    try:
        health_res = client.health()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Server Health Check: {health_res}")
    except Exception as exc:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cảnh báo Health check: {exc}")

    # Register
    reg_res = client.register()
    if reg_res.get("status") != "ok":
        print(f"[{datetime.now().strftime('%H:%M:%S')}] LỖI: Đăng ký thất bại: {reg_res.get('message', 'Không kết nối được server')}")
        sys.exit(1)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] REGISTER: {reg_res.get('message', 'OK')}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Đang gửi dữ liệu định kỳ mỗi {interval}s (Bấm Ctrl+C để dừng)...")

    try:
        while True:
            metrics = client.collect_system_metrics()
            metric_res = client.send_metrics(**metrics)
            if was_disconnected_by_server(metric_res):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Server đã ngắt client; dừng giám sát.")
                return
            hb_res = client.send_heartbeat()
            if was_disconnected_by_server(hb_res):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Server đã ngắt client; dừng giám sát.")
                return

            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"CPU={metrics['cpu']:>5.1f}% | "
                f"RAM={metrics['ram']:>5.1f}% | "
                f"DISK={metrics['disk']:>5.1f}% | "
                f"NET={metrics['network']:>5.1f}% | "
                f"Send={metric_res.get('status')} | "
                f"HB={hb_res.get('status')}"
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Nhận tín hiệu dừng, gửi lệnh LOGOUT...")
        try:
            logout_res = client.disconnect()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] LOGOUT: {logout_res.get('message', 'OK')}")
        except Exception as exc:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Lỗi gửi LOGOUT: {exc}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Client Agent đã dừng an toàn.")


# ============================================================================
# 4. ENTRY POINT
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Network Monitoring System - Client Agent (Hỗ trợ cả GUI và CLI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ sử dụng:
  1. Khởi chạy Giao diện đồ họa (GUI mặc định):
     python client/monitoring_client.py
     python client/monitoring_client.py --gui

  2. Khởi chạy chế độ dòng lệnh (CLI chạy ngầm):
     python client/monitoring_client.py PC01 --cli
     python client/monitoring_client.py PC01 --cli --host 127.0.0.1 --port 8888 --interval 3
""",
    )
    default_name = socket.gethostname() if socket.gethostname() else "Node-01"
    parser.add_argument("name", nargs="?", default=None, help=f"Tên Client (mặc định: {default_name})")
    parser.add_argument("--host", default="127.0.0.1", help="Địa chỉ IP Server (mặc định: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_TCP_PORT, help=f"Cổng TCP Server (mặc định: {DEFAULT_TCP_PORT})")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help=f"Cổng HTTP Server (mặc định: {DEFAULT_HTTP_PORT})")
    parser.add_argument("--interval", type=int, default=3, help="Chu kỳ gửi dữ liệu (giây) (mặc định: 3)")
    parser.add_argument("--cli", action="store_true", help="Chạy ở chế độ dòng lệnh (CLI / headless)")
    parser.add_argument("--gui", action="store_true", help="Chạy ở chế độ giao diện đồ họa Desktop (GUI)")

    args = parser.parse_args()
    client_name = args.name.strip() if args.name else default_name

    # Determine execution mode:
    # - If --cli is explicitly passed, run CLI mode.
    # - If a positional client name was passed without --gui, run CLI mode.
    # - Otherwise (no extra flags, or --gui was specified), run GUI mode if Tkinter is available.
    is_cli_requested = args.cli or (args.name is not None and not args.gui)

    if not is_cli_requested and TKINTER_AVAILABLE:
        app = ClientGUI(
            name=client_name,
            host=args.host,
            tcp_port=args.port,
            http_port=args.http_port,
            interval=args.interval,
        )
        app.run()
    else:
        if not is_cli_requested and not TKINTER_AVAILABLE:
            print("[CẢNH BÁO] Tkinter không khả dụng trong môi trường này. Tự động chuyển sang chế độ CLI...")
        run_cli(
            name=client_name,
            host=args.host,
            port=args.port,
            http_port=args.http_port,
            interval=max(1, args.interval),
        )


if __name__ == "__main__":
    main()
