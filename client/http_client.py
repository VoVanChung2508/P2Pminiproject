import json
from urllib import request, error


class HTTPClient:
    def __init__(self, base_url: str = "http://127.0.0.1:9001"):
        self.base_url = base_url.rstrip("/")

    def get(self, endpoint: str):
        try:
            with request.urlopen(f"{self.base_url}{endpoint}") as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return {"status": "error", "code": exc.code, "message": exc.reason}

    def post(self, endpoint: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(f"{self.base_url}{endpoint}", data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return {"status": "error", "code": exc.code, "message": exc.reason}

    def health(self):
        return self.get("/health")

    def list_peers(self):
        return self.get("/peers")

    def register(self, name: str, host: str, port: int):
        return self.post("/register", {"name": name, "host": host, "port": port})
