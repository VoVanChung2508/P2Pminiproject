from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from os import environ
from typing import Any

from flask import Flask, jsonify, render_template_string


HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(environ.get("MONITOR_HTTP_PORT", "8080"))
TCP_HOST = "0.0.0.0"
TCP_PORT = int(environ.get("MONITOR_TCP_PORT", "8888"))
HEARTBEAT_TIMEOUT = 15
SAMPLE_LIMIT = 120

app = Flask(__name__)
state_lock = threading.RLock()
clients: dict[str, dict[str, Any]] = {}
history: dict[str, list[dict[str, Any]]] = {}
alerts: list[dict[str, Any]] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def client_snapshot(client: dict[str, Any]) -> dict[str, Any]:
    result = dict(client)
    result["last_seen"] = result["last_seen"].isoformat(timespec="seconds")
    result["seconds_since_heartbeat"] = round(
        max(0.0, time.time() - result.pop("last_seen_epoch")), 1
    )
    return result


def parse_metric(value: str, name: str) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return round(number, 1)


def register_client(name: str, ip: str) -> None:
    key = name.lower()
    with state_lock:
        existing = clients.get(key)
        clients[key] = {
            "name": name,
            "ip": ip,
            "cpu": existing.get("cpu", 0) if existing else 0,
            "ram": existing.get("ram", 0) if existing else 0,
            "disk": existing.get("disk", 0) if existing else 0,
            "network": existing.get("network", 0) if existing else 0,
            "status": "ONLINE",
            "last_seen": datetime.now(timezone.utc),
            "last_seen_epoch": time.time(),
            "registered_at": existing.get("registered_at", now_iso()) if existing else now_iso(),
        }
    log(f"{name} registered from {ip}")


def update_system(name: str, metrics: dict[str, float]) -> None:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        if client is None:
            return
        client.update(metrics)
        client["status"] = "ONLINE"
        client["last_seen"] = datetime.now(timezone.utc)
        client["last_seen_epoch"] = time.time()
        sample = {"timestamp": now_iso(), **metrics}
        history.setdefault(key, []).append(sample)
        history[key] = history[key][-SAMPLE_LIMIT:]
        for metric, limit in (("cpu", 80), ("ram", 80), ("disk", 90)):
            if metrics.get(metric, 0) > limit:
                alerts.append(
                    {
                        "timestamp": sample["timestamp"],
                        "client": name,
                        "metric": metric.upper(),
                        "value": metrics[metric],
                        "limit": limit,
                    }
                )
        del alerts[:-100]


def touch_client(name: str) -> None:
    with state_lock:
        client = clients.get(name.lower())
        if client:
            client["status"] = "ONLINE"
            client["last_seen"] = datetime.now(timezone.utc)
            client["last_seen_epoch"] = time.time()


def mark_offline_clients() -> None:
    while True:
        time.sleep(2)
        current_time = time.time()
        with state_lock:
            for client in clients.values():
                if current_time - client["last_seen_epoch"] > HEARTBEAT_TIMEOUT:
                    if client["status"] != "OFFLINE":
                        log(f"{client['name']} -> OFFLINE")
                    client["status"] = "OFFLINE"


def handle_message(message: str, address: tuple[str, int]) -> str:
    parts = [part.strip() for part in message.strip().split("|")]
    if not parts:
        return "ERROR|Empty message"
    command = parts[0].upper()
    try:
        if command == "REGISTER" and len(parts) >= 2:
            register_client(parts[1], address[0])
            return "OK|REGISTERED"
        if command == "HEARTBEAT" and len(parts) >= 2:
            touch_client(parts[1])
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
            update_system(parts[1], metrics)
            return "OK|SYSTEM"
        if command == "LOGOUT" and len(parts) >= 2:
            with state_lock:
                if parts[1].lower() in clients:
                    clients[parts[1].lower()]["status"] = "OFFLINE"
            return "OK|LOGOUT"
        return "ERROR|Unsupported message"
    except (ValueError, IndexError) as exc:
        return f"ERROR|{exc}"


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


def ensure_ports_available() -> None:
    for port, service in ((TCP_PORT, "TCP"), (HTTP_PORT, "HTTP")):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as exc:
                raise SystemExit(
                    f"Cannot start {service} service on port {port}: {exc}. "
                    "Set MONITOR_HTTP_PORT and MONITOR_TCP_PORT to free ports."
                ) from exc


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        tcp_port=TCP_PORT,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
    )


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "tcp_port": TCP_PORT, "http_port": HTTP_PORT})


@app.route("/api/clients")
def api_clients():
    with state_lock:
        result = [client_snapshot(client) for client in clients.values()]
    return jsonify({"clients": result})


@app.route("/api/clients/<name>/history")
def api_client_history(name: str):
    with state_lock:
        return jsonify({"client": name, "samples": history.get(name.lower(), [])})


@app.route("/api/alerts")
def api_alerts():
    with state_lock:
        return jsonify({"alerts": list(reversed(alerts))})


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Network Monitor</title>
<style>
body{margin:0;background:#08111f;color:#e5e7eb;font:15px Arial,sans-serif}
.wrap{max-width:1100px;margin:28px auto;padding:0 18px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #26364d;padding-bottom:18px}
h1{margin:0;color:#67e8f9;letter-spacing:2px}.meta{color:#94a3b8}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-top:20px}
.panel{background:#101c2e;border:1px solid #26364d;border-radius:10px;padding:18px}
h2{font-size:17px;margin:0 0 12px}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:12px 8px;border-bottom:1px solid #26364d}
th{color:#94a3b8;font-size:12px;text-transform:uppercase}
.online{color:#4ade80}.offline{color:#f87171}.alert{border-left:3px solid #f59e0b;background:#211b11;padding:9px;margin:8px 0}
.empty{color:#94a3b8}.bar{height:6px;background:#26364d;border-radius:4px}.fill{height:100%;background:#22d3ee;border-radius:4px}
@media(max-width:800px){.grid{grid-template-columns:1fr}table{font-size:13px}th:nth-child(2),td:nth-child(2){display:none}}
</style>
</head>
<body><main class="wrap"><header><h1>NETWORK MONITOR</h1>
<div class="meta">TCP {{ tcp_port }} · heartbeat {{ heartbeat_timeout }}s</div></header>
<div class="grid"><section class="panel"><h2>Clients</h2>
<table><thead><tr><th>Client</th><th>IP</th><th>CPU</th><th>RAM</th><th>Disk</th><th>Status</th></tr></thead>
<tbody id="clients"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody></table></section>
<section class="panel"><h2>Alerts</h2><div id="alerts" class="empty">Loading...</div></section></div></main>
<script>
function metric(value){return `${value}% <div class="bar"><div class="fill" style="width:${value}%"></div></div>`}
async function refresh(){
 const clientsResponse=await fetch('/api/clients');
 const alertsResponse=await fetch('/api/alerts');
 const data=(await clientsResponse.json()).clients;
 document.querySelector('#clients').innerHTML=data.length?data.map(c=>`<tr><td><b>${c.name}</b></td><td>${c.ip}</td><td>${metric(c.cpu)}</td><td>${metric(c.ram)}</td><td>${metric(c.disk)}</td><td class="${c.status==='ONLINE'?'online':'offline'}">${c.status}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">No clients registered</td></tr>';
 const items=(await alertsResponse.json()).alerts;
 document.querySelector('#alerts').innerHTML=items.length?items.slice(0,12).map(a=>`<div class="alert">${a.client}: ${a.metric} = ${a.value}%</div>`).join(''):'<div class="empty">No alerts</div>';
}
refresh();setInterval(refresh,3000);
</script></body></html>
"""


if __name__ == "__main__":
    ensure_ports_available()
    threading.Thread(target=tcp_server, daemon=True).start()
    threading.Thread(target=mark_offline_clients, daemon=True).start()
    log(f"HTTP dashboard available at http://localhost:{HTTP_PORT}")
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, threaded=True)
