from client.http_client import HTTPClient
from client.tcp_client import TCPClient


class P2PClient:
    def __init__(self, name: str, host: str = "192.168.100.200", tcp_port: int = 8888, http_port: int = 9001):
        self.name = name
        self.tcp_client = TCPClient(host=host, port=tcp_port)
        self.http_client = HTTPClient(f"http://{host}:{http_port}")

    def register(self):
        response = self.tcp_client.register(self.name, host=self.tcp_client.host, port=self.tcp_client.port)
        return response

    def list_peers(self):
        return self.tcp_client.list_peers()

    def health(self):
        return self.http_client.health()

    def ping(self):
        return self.tcp_client.ping()

    def disconnect(self):
        return self.tcp_client.disconnect()


if __name__ == "__main__":
    client = P2PClient("client_01")
    print(client.health())
    print(client.register())
    print(client.list_peers())
