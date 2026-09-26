import json
import os
from urllib import error, request

DEFAULT_HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "8081"))


class HTTPClient:
    def __init__(self, base_url: str = None):
        if base_url is None:
            base_url = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}"
        self.base_url = base_url.rstrip("/")

    def get(self, endpoint: str):
        try:
            with request.urlopen(f"{self.base_url}{endpoint}") as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {"status": "ok"}
        except error.HTTPError as exc:
            return {"status": "error", "code": exc.code, "message": exc.reason}
        except error.URLError as exc:
            return {"status": "error", "message": str(exc.reason)}

    def post(self, endpoint: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {"status": "ok"}
        except error.HTTPError as exc:
            return {"status": "error", "code": exc.code, "message": exc.reason}
        except error.URLError as exc:
            return {"status": "error", "message": str(exc.reason)}

    def health(self):
        return self.get("/api/health")

    def list_clients(self):
        return self.get("/api/clients")

    def list_peers(self):
        return self.list_clients()

    def register(self, name: str, host: str, port: int):
        return {"status": "not_used", "message": "Use TCP REGISTER to the monitoring server"}
