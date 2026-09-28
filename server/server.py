from __future__ import annotations

import socket
import threading
import time
import hmac
from datetime import datetime
from os import environ
from typing import Any

from flask import Flask, jsonify, render_template_string, request
from common.database import DatabaseManager

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(environ.get("MONITOR_HTTP_PORT", "8081"))
TCP_HOST = "0.0.0.0"
TCP_PORT = int(environ.get("MONITOR_TCP_PORT", "8888"))
HEARTBEAT_TIMEOUT = 15
SAMPLE_LIMIT = 120
ADMIN_TOKEN = environ.get("MONITOR_ADMIN_TOKEN", "")

app = Flask(__name__)
state_lock = threading.RLock()
clients: dict[str, dict[str, Any]] = {}
disconnected_clients: set[str] = set()

db_manager = DatabaseManager()


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def client_snapshot(client: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(client)
    runtime = runtime or {}
    result["seconds_since_heartbeat"] = round(
        max(0.0, time.time() - runtime["last_seen_epoch"]), 1
    ) if runtime.get("last_seen_epoch") is not None else None
    return result


def parse_metric(value: str, name: str) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return round(number, 1)


def register_client(name: str, ip: str) -> None:
    key = name.lower()
    if not db_manager.register_client(name, ip):
        raise RuntimeError("MySQL did not save the client registration.")
    with state_lock:
        disconnected_clients.discard(key)
        clients[key] = {
            "name": name,
            "ip": ip,
            "status": "ONLINE",
            "last_seen_epoch": time.time(),
        }
    log(f"{name} registered from {ip}")


def update_system(name: str, metrics: dict[str, float]) -> bool:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        if client is None or key in disconnected_clients:
            return False
    if not db_manager.update_metrics(name, metrics):
        raise RuntimeError("MySQL did not save the client metrics.")
    for metric, limit in (("cpu", 80), ("ram", 80), ("disk", 90)):
        if metrics.get(metric, 0) > limit and not db_manager.add_alert(
            name, metric, metrics[metric], float(limit)
        ):
            raise RuntimeError("MySQL did not save the generated alert.")
    with state_lock:
        client = clients.get(key)
        if client is None or key in disconnected_clients:
            return False
        client["status"] = "ONLINE"
        client["last_seen_epoch"] = time.time()
    return True


def touch_client(name: str) -> bool:
    with state_lock:
        key = name.lower()
        client = clients.get(key)
        if not client or key in disconnected_clients:
            return False
    if not db_manager.update_heartbeat(name):
        raise RuntimeError("MySQL did not save the client heartbeat.")
    with state_lock:
        client = clients.get(key)
        if client and key not in disconnected_clients:
            client["status"] = "ONLINE"
            client["last_seen_epoch"] = time.time()
            return True
    return False


def is_client_disconnected(name: str) -> bool:
    with state_lock:
        return name.lower() in disconnected_clients


def disconnect_client(name: str) -> bool:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        if client is None:
            return False
    if not db_manager.update_status(client["name"], "OFFLINE"):
        raise RuntimeError("MySQL did not save the client status.")
    with state_lock:
        disconnected_clients.add(key)
        client["status"] = "OFFLINE"
    log(f"{client['name']} disconnected by server administrator")
    return True


def mark_offline_clients() -> None:
    while True:
        time.sleep(2)
        current_time = time.time()
        with state_lock:
            for client in clients.values():
                if current_time - client["last_seen_epoch"] > HEARTBEAT_TIMEOUT:
                    if client["status"] != "OFFLINE":
                        log(f"{client['name']} -> OFFLINE")
                        if not db_manager.update_status(client['name'], "OFFLINE"):
                            log("ERROR: Could not save OFFLINE status to MySQL.")
                    client["status"] = "OFFLINE"


def handle_message(message: str, address: tuple[str, int]) -> str:
    parts = [part.strip() for part in message.strip().split("|")]
    if not parts:
        return "ERROR|Empty message"
    command = parts[0].upper()
    try:
        if command == "REGISTER" and len(parts) >= 2:
            if not parts[1]:
                return "ERROR|Client name is required"
            register_client(parts[1], address[0])
            return "OK|REGISTERED"
        if len(parts) >= 2 and is_client_disconnected(parts[1]):
            return "ERROR|DISCONNECTED"
        if command == "HEARTBEAT" and len(parts) >= 2:
            if not touch_client(parts[1]):
                reason = "DISCONNECTED" if is_client_disconnected(parts[1]) else "NOT_REGISTERED"
                return f"ERROR|{reason}"
            return "OK|HEARTBEAT"
        if command == "SYSTEM" and len(parts) >= 2:
            metrics: dict[str, float] = {}
            for item in parts[2:]:
                key, value = item.split("=", 1)
                metric_name = key.lower()
                if metric_name in {"cpu", "ram", "disk", "network"}:
                    metrics[metric_name] = parse_metric(value.rstrip("%"), metric_name)
            if not metrics:
                return "ERROR|No metrics supplied"
            if not update_system(parts[1], metrics):
                reason = "DISCONNECTED" if is_client_disconnected(parts[1]) else "NOT_REGISTERED"
                return f"ERROR|{reason}"
            return "OK|SYSTEM"
        if command == "LOGOUT" and len(parts) >= 2:
            with state_lock:
                client = clients.get(parts[1].lower())
            if client:
                if not db_manager.update_status(parts[1], "OFFLINE"):
                    raise RuntimeError("MySQL did not save the client logout.")
                with state_lock:
                    client["status"] = "OFFLINE"
            return "OK|LOGOUT"
        return "ERROR|Unsupported message"
    except (ValueError, IndexError) as exc:

        return f"ERROR|{exc}"
    except RuntimeError as exc:
        log(f"Database operation failed: {exc}")
        return "ERROR|DATABASE_UNAVAILABLE"


def tcp_client_session(connection: socket.socket, address: tuple[str, int]) -> None:
    with connection:
        connection.settimeout(30)
        buffer = ""
        while True:
            try:
                data = connection.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            buffer += data.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    response = handle_message(line, address)
                    connection.sendall((response + "\n").encode("utf-8"))


def tcp_server() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((TCP_HOST, TCP_PORT))
        server_socket.listen(50)
        log(f"Monitoring TCP server listening on {TCP_PORT}")
        while True:
            connection, address = server_socket.accept()
            threading.Thread(
                target=tcp_client_session, args=(connection, address), daemon=True
            ).start()


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
    except OSError:
        return False


def find_available_port(preferred: int, host: str = "0.0.0.0", max_attempts: int = 50) -> int:
    for p in range(preferred, preferred + max_attempts):
        if is_port_available(p, host):
            return p
    return preferred


def ensure_ports_available() -> None:
    global HTTP_PORT, TCP_PORT
    for service, port_var_name in (("TCP", "TCP_PORT"), ("HTTP", "HTTP_PORT")):
        port = globals()[port_var_name]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as exc:
                env_key = f"MONITOR_{service}_PORT"
                free_port = find_available_port(8081 if port == 8080 else port + 1)
                if env_key not in environ:
                    log(f"Port {port} for {service} is occupied ({exc}). Auto-switching to port {free_port}.")
                    globals()[port_var_name] = free_port
                    continue
                raise SystemExit(
                    f"Cannot start {service} service on port {port}: {exc}. "
                    f"Set {env_key} to a free port (e.g., {free_port})."
                ) from exc


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        tcp_port=TCP_PORT,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        admin_disconnect_enabled=bool(ADMIN_TOKEN),
    )


@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "project": "Network Monitoring System",
        "tcp_port": TCP_PORT,
        "http_port": HTTP_PORT,
        "storage": "mysql" if db_manager.is_connected else "unavailable",
        "admin_disconnect_enabled": bool(ADMIN_TOKEN),
    })


@app.route("/api/clients")
def api_clients():
    try:
        stored_clients = db_manager.get_clients()
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    with state_lock:
        result = [
            client_snapshot(
                client,
                clients.get(str(client["name"]).lower()),
            )
            for client in stored_clients
        ]
    return jsonify({"clients": result})


@app.route("/api/clients/<name>/history")
def api_client_history(name: str):
    try:
        samples = db_manager.get_history(name, SAMPLE_LIMIT)
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"client": name, "samples": samples})


@app.route("/api/alerts")
def api_alerts():
    try:
        stored_alerts = db_manager.get_alerts(100)
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"alerts": stored_alerts})


@app.post("/api/clients/<name>/disconnect")
def api_disconnect_client(name: str):
    if not ADMIN_TOKEN:
        return jsonify({"status": "error", "message": "Server admin token is not configured"}), 503
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(supplied_token, ADMIN_TOKEN):
        return jsonify({"status": "error", "message": "Invalid admin token"}), 401
    if not disconnect_client(name):
        return jsonify({"status": "error", "message": "Client not found"}), 404
    return jsonify({"status": "ok", "message": f"Client {name} disconnected"})


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Network Monitoring System</title>
<style>
:root{--bg:#090d16;--card:#111927;--border:#1e293b;--primary:#38bdf8;--success:#22c55e;--danger:#ef4444;--warning:#f59e0b;--text:#f1f5f9;--subtext:#94a3b8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:1300px;margin:24px auto;padding:0 20px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:18px;margin-bottom:20px}
.title-group h1{margin:0;font-size:22px;letter-spacing:1px;color:var(--primary);font-weight:700}
.title-group .sub{margin:4px 0 0;color:var(--subtext);font-size:13px}
.meta-badges{display:flex;gap:10px}
.badge{background:#1e293b;border:1px solid #334155;padding:5px 12px;border-radius:20px;font-size:12px;color:#cbd5e1}
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column}
.stat-card .label{font-size:12px;color:var(--subtext);text-transform:uppercase;font-weight:600}
.stat-card .val{font-size:26px;font-weight:700;margin-top:6px;color:var(--primary)}
.chart-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.chart-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.chart-head h3{margin:0;font-size:16px}
.chart-value{display:inline-block;padding:5px 10px;border-radius:999px;background:rgba(56,189,248,.10);color:#bae6fd;border:1px solid rgba(56,189,248,.35);font-weight:700}
.chart-card canvas{display:block;width:100%;height:180px;border-radius:8px;border:1px solid #1f2937;background:linear-gradient(180deg,#0f172a,#020817)}
.main-grid{display:grid;grid-template-columns:2.5fr 1fr;gap:20px}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
.panel h2{font-size:16px;margin:0 0 14px;color:#e2e8f0;display:flex;align-items:center;justify-content:space-between}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--border)}
th{color:var(--subtext);font-size:11px;text-transform:uppercase;letter-spacing:0.5px}
.client-row{cursor:pointer;transition:background .2s ease}
.client-row:hover{background:rgba(56,189,248,.05)}
.client-row.selected{background:rgba(56,189,248,.12)}
.online-tag{color:var(--success);font-weight:600;display:inline-flex;align-items:center;gap:6px}
.online-tag::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--success)}
.offline-tag{color:var(--danger);font-weight:600;display:inline-flex;align-items:center;gap:6px}
.offline-tag::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--danger)}
.bar-wrap{display:flex;align-items:center;gap:8px}
.bar{height:6px;background:#1e293b;border-radius:4px;flex:1;overflow:hidden}
.fill{height:100%;background:var(--primary);border-radius:4px;transition:width 0.4s ease}
.fill.warn{background:var(--warning)}
.fill.high{background:var(--danger)}
.alert-item{border-left:3px solid var(--warning);background:rgba(245,158,11,0.08);padding:10px 12px;border-radius:0 6px 6px 0;margin-bottom:8px;font-size:13px}
.alert-time{font-size:11px;color:var(--subtext);margin-top:4px}
.disconnect-button{background:#7f1d1d;color:#fee2e2;border:1px solid #b91c1c;padding:5px 9px;border-radius:6px;cursor:pointer}
.disconnect-button:disabled{opacity:.45;cursor:not-allowed}
.empty{color:var(--subtext);text-align:center;padding:24px 0}
@media(max-width:900px){.main-grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:1fr}.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="title-group">
      <h1>NETWORK MONITORING SYSTEM</h1>
      <p class="sub">Hệ thống Giám sát Mạng & Thiết bị Phân tán theo thời gian thực</p>
    </div>
    <div class="meta-badges">
      <span class="badge">TCP: {{ tcp_port }}</span>
      <span class="badge">Heartbeat: {{ heartbeat_timeout }}s</span>
      <span class="badge">Admin: {{ 'enabled' if admin_disconnect_enabled else 'disabled' }}</span>
      <span class="badge" id="last-sync">Sync: Đang tải...</span>
    </div>
  </header>

  <div class="stats-grid">
    <div class="stat-card"><span class="label">Tổng số Nodes</span><span class="val" id="stat-total">0</span></div>
    <div class="stat-card"><span class="label">Nodes Đang Online</span><span class="val" style="color:var(--success)" id="stat-online">0</span></div>
    <div class="stat-card"><span class="label">Cảnh báo Vượt ngưỡng</span><span class="val" style="color:var(--warning)" id="stat-alerts">0</span></div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-head"><h3>CPU</h3><span class="chart-value" id="cpu-value">0%</span></div>
      <canvas id="cpu-chart" width="320" height="180"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head"><h3>RAM</h3><span class="chart-value" id="ram-value">0%</span></div>
      <canvas id="ram-chart" width="320" height="180"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head"><h3>Network Traffic</h3><span class="chart-value" id="network-value">0%</span></div>
      <canvas id="network-chart" width="320" height="180"></canvas>
    </div>
  </div>

  <div class="main-grid">
    <section class="panel">
      <h2><span>Danh sách Thiết bị / Nodes</span><span style="font-size:12px;color:var(--subtext);font-weight:normal" id="nodes-count"></span></h2>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>IP Address</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>Disk</th>
              <th>Network</th>
              <th>Trạng thái</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody id="clients">
            <tr><td colspan="8" class="empty">Đang kết nối tới máy chủ...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2><span>Nhật ký Cảnh báo (Alerts)</span></h2>
      <div id="alerts"><div class="empty">Không có cảnh báo</div></div>
    </section>
  </div>
</div>

<script>
const chartSettings = {
  cpu: { color: '#38bdf8', valueEl: 'cpu-value', canvasId: 'cpu-chart' },
  ram: { color: '#22c55e', valueEl: 'ram-value', canvasId: 'ram-chart' },
  network: { color: '#a78bfa', valueEl: 'network-value', canvasId: 'network-chart' }
};
let selectedClientName = null;
const adminDisconnectEnabled = {{ 'true' if admin_disconnect_enabled else 'false' }};
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

function renderMetric(val) {
  const num = parseFloat(val) || 0;
  const cls = num > 90 ? 'high' : (num > 75 ? 'warn' : '');
  return `<div class="bar-wrap"><span>${num}%</span><div class="bar"><div class="fill ${cls}" style="width:${Math.min(100, num)}%"></div></div></div>`;
}

function normalizeHistory(samples = []) {
  return samples.slice(-30).map((sample) => ({
    cpu: Number(sample.cpu || 0),
    ram: Number(sample.ram || 0),
    network: Number(sample.network || 0),
    timestamp: sample.timestamp || Date.now()
  }));
}

function drawChart(canvasId, values, color) {
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const width = canvas.width = Math.max(canvas.clientWidth * 2, 320);
  const height = canvas.height = 180 * 2;
  const pad = 18;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#020817';
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = 'rgba(148,163,184,0.18)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + ((height - pad * 2) / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
  }

  if (!values || values.length === 0) {
    ctx.fillStyle = '#94a3b8';
    ctx.font = '14px Segoe UI';
    ctx.fillText('Chưa có dữ liệu', pad + 8, height / 2);
    return;
  }

  const data = values.map(v => Number(v) || 0);
  const maxVal = Math.max(100, ...data, 10);
  const stepX = (width - pad * 2) / Math.max(data.length - 1, 1);

  ctx.beginPath();
  data.forEach((value, index) => {
    const x = pad + index * stepX;
    const y = height - pad - ((value / maxVal) * (height - pad * 2));
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.shadowColor = color;
  ctx.shadowBlur = 10;
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function renderCharts(historySamples) {
  const history = normalizeHistory(historySamples);
  const cpuValues = history.map(item => item.cpu);
  const ramValues = history.map(item => item.ram);
  const networkValues = history.map(item => item.network);

  const cpuCurrent = cpuValues.length ? cpuValues[cpuValues.length - 1] : 0;
  const ramCurrent = ramValues.length ? ramValues[ramValues.length - 1] : 0;
  const networkCurrent = networkValues.length ? networkValues[networkValues.length - 1] : 0;

  document.getElementById('cpu-value').textContent = `${cpuCurrent.toFixed(0)}%`;
  document.getElementById('ram-value').textContent = `${ramCurrent.toFixed(0)}%`;
  document.getElementById('network-value').textContent = `${networkCurrent.toFixed(0)}%`;

  drawChart('cpu-chart', cpuValues, '#38bdf8');
  drawChart('ram-chart', ramValues, '#22c55e');
  drawChart('network-chart', networkValues, '#a78bfa');
}

async function fetchClientHistory(clientName) {
  if (!clientName) return [];
  try {
    const res = await fetch(`/api/clients/${encodeURIComponent(clientName)}/history`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.samples || [];
  } catch (error) {
    return [];
  }
}

function updateSelection(clients) {
  if (!clients || clients.length === 0) {
    selectedClientName = null;
    return;
  }

  const available = clients.map(c => c.name);
  if (!selectedClientName || !available.includes(selectedClientName)) {
    const preferredOnline = clients.find(c => c.status === 'ONLINE');
    selectedClientName = preferredOnline ? preferredOnline.name : clients[0].name;
  }
}

async function refresh() {
  try {
    const [clientsRes, alertsRes] = await Promise.all([
      fetch('/api/clients'),
      fetch('/api/alerts')
    ]);
    const clientsData = (await clientsRes.json()).clients || [];
    const alertsData = (await alertsRes.json()).alerts || [];

    updateSelection(clientsData);

    const onlineCount = clientsData.filter(c => c.status === 'ONLINE').length;
    document.getElementById('stat-total').textContent = clientsData.length;
    document.getElementById('stat-online').textContent = onlineCount;
    document.getElementById('stat-alerts').textContent = alertsData.length;
    document.getElementById('nodes-count').textContent = `${onlineCount}/${clientsData.length} online`;

    const clientsTbody = document.getElementById('clients');
    if (clientsData.length === 0) {
      clientsTbody.innerHTML = '<tr><td colspan="8" class="empty">Chưa có thiết bị nào đăng ký</td></tr>';
      renderCharts([]);
    } else {
      clientsTbody.innerHTML = clientsData.map(c => `
        <tr class="client-row ${selectedClientName === c.name ? 'selected' : ''}" data-client-name="${escapeHtml(c.name)}">
          <td><strong>${escapeHtml(c.name)}</strong></td>
          <td><code>${escapeHtml(c.ip)}</code></td>
          <td style="min-width:110px">${renderMetric(c.cpu)}</td>
          <td style="min-width:110px">${renderMetric(c.ram)}</td>
          <td style="min-width:110px">${renderMetric(c.disk)}</td>
          <td style="min-width:110px">${renderMetric(c.network)}</td>
          <td><span class="${c.status === 'ONLINE' ? 'online-tag' : 'offline-tag'}">${escapeHtml(c.status)}</span></td>
          <td><button class="disconnect-button" type="button" ${c.status !== 'ONLINE' || !adminDisconnectEnabled ? 'disabled' : ''}>Ngắt</button></td>
        </tr>
      `).join('');
      clientsTbody.querySelectorAll('tr[data-client-name]').forEach(row => {
        row.addEventListener('click', () => selectClient(row.dataset.clientName));
        row.querySelector('.disconnect-button').addEventListener('click', event => {
          event.stopPropagation();
          disconnectClient(row.dataset.clientName);
        });
      });

      const selectedHistory = await fetchClientHistory(selectedClientName);
      renderCharts(selectedHistory);
    }

    const alertsBox = document.getElementById('alerts');
    if (alertsData.length === 0) {
      alertsBox.innerHTML = '<div class="empty">Hệ thống bình thường, không có cảnh báo</div>';
    } else {
      alertsBox.innerHTML = alertsData.slice(0, 10).map(a => `
        <div class="alert-item">
          <div><strong>${escapeHtml(a.client)}</strong>: Chỉ số ${escapeHtml(a.metric)} vượt ngưỡng (<b>${escapeHtml(a.value)}%</b> &gt; ${escapeHtml(a.limit)}%)</div>
          <div class="alert-time">${escapeHtml(a.timestamp)}</div>
        </div>
      `).join('');
    }

    const now = new Date();
    document.getElementById('last-sync').textContent = 'Sync: ' + now.toLocaleTimeString();
  } catch (err) {
    document.getElementById('last-sync').textContent = 'Mất kết nối API';
  }
}

function selectClient(clientName) {
  selectedClientName = clientName;
  refresh();
}

async function disconnectClient(clientName) {
  if (!window.confirm(`Ngắt kết nối client "${clientName}"?`)) return;
  const token = window.prompt('Nhập MONITOR_ADMIN_TOKEN:');
  if (token === null) return;
  try {
    const response = await fetch(`/api/clients/${encodeURIComponent(clientName)}/disconnect`, {
      method: 'POST',
      headers: { 'X-Admin-Token': token }
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || `HTTP ${response.status}`);
    window.alert(result.message);
    refresh();
  } catch (error) {
    window.alert(`Không thể ngắt kết nối client: ${error.message}`);
  }
}

refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


def start_services() -> None:
    """Start TCP monitoring, offline checker, and HTTP dashboard.

    This module contains server services only; it never creates the desktop GUI.
    """
    ensure_ports_available()
    log(f"Starting monitoring services: TCP={TCP_PORT}, HTTP={HTTP_PORT}")
    if not db_manager.connect():
        raise SystemExit("MySQL is required; server will not start without persistent database storage.")
    if not db_manager.mark_all_clients_offline():
        db_manager.close()
        raise SystemExit("Could not initialize client statuses in MySQL; server will not start.")
    if not ADMIN_TOKEN:
        log("WARNING: MONITOR_ADMIN_TOKEN is not configured; dashboard disconnect is disabled.")

    threading.Thread(target=mark_offline_clients, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()

    log(f"HTTP dashboard listening on {HTTP_PORT}")
    app.run(
        host=HTTP_HOST,
        port=HTTP_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    start_services()
