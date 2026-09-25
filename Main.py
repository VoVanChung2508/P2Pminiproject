from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

PROJECT_NAME = "P2P Mini Project"
PROJECT_ROOT = Path(__file__).resolve().parent
MODULES = [
    "Main.py",
    "server.py",
    "common/__init__.py",
    "common/file_descriptor.py",
    "common/message_protocol.py",
    "gui/main_gui.py",
    "peer/p2p_client_manager.py",
    "peer/p2p_server.py",
    "peer/shared_file_manager.py",
    "server/client_handler.py",
    "server/peer_info.py",
    "server/server_main.py",
]


@app.route("/")
def home():
    return render_template_string(
        """
        <!doctype html>
        <html lang="vi">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{{ project_name }}</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    margin: 0;
                    background: linear-gradient(135deg, #0f172a, #111827, #1f2937);
                    color: #e5e7eb;
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .container {
                    width: min(900px, 90vw);
                    background: rgba(17, 24, 39, 0.9);
                    border: 1px solid rgba(148, 163, 184, 0.3);
                    border-radius: 18px;
                    padding: 32px;
                    box-shadow: 0 20px 50px rgba(0,0,0,0.35);
                }
                h1 {
                    margin-top: 0;
                    color: #7dd3fc;
                }
                .status {
                    display: inline-block;
                    background: #14532d;
                    color: #dcfce7;
                    padding: 8px 12px;
                    border-radius: 999px;
                    font-weight: bold;
                    margin-bottom: 20px;
                }
                ul {
                    padding-left: 18px;
                    line-height: 1.8;
                }
                code {
                    background: rgba(148, 163, 184, 0.15);
                    padding: 2px 6px;
                    border-radius: 6px;
                }
                .api-link {
                    display: inline-block;
                    margin-top: 16px;
                    text-decoration: none;
                    background: #2563eb;
                    color: white;
                    padding: 10px 18px;
                    border-radius: 10px;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="status">Server đang chạy</div>
                <h1>{{ project_name }}</h1>
                <p>Project này đã được chuyển thành 1 web server bằng Flask.</p>
                <p>Danh sách module chính:</p>
                <ul>
                    {% for item in modules %}
                    <li><code>{{ item }}</code></li>
                    {% endfor %}
                </ul>
                <p>API kiểm tra trạng thái: <code>/api/health</code></p>
                <p>API danh sách module: <code>/api/modules</code></p>
                <a class="api-link" href="/api/health">Kiểm tra trạng thái API</a>
            </div>
        </body>
        </html>
        """,
        project_name=PROJECT_NAME,
        modules=MODULES,
    )


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "status": "ok",
            "project": PROJECT_NAME,
            "message": "Web server is running successfully",
            "server": "Flask",
            "port": 5000,
        }
    )


@app.route("/api/modules")
def api_modules():
    return jsonify({"project": PROJECT_NAME, "modules": MODULES})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)