from __future__ import annotations

import argparse
import socket
import time

import psutil



def send_message(connection: socket.socket, message: str) -> str:
    connection.sendall((message + "\n").encode("utf-8"))
    return connection.recv(4096).decode("utf-8", errors="replace").strip()


def system_message(name: str) -> str:
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    network = min(100.0, psutil.net_io_counters().bytes_sent / (1024 * 1024))
    return f"SYSTEM|{name}|CPU={cpu:.1f}|RAM={ram:.1f}|DISK={disk:.1f}|NETWORK={network:.1f}"


def run(name: str, host: str, port: int, interval: int) -> None:
    with socket.create_connection((host, port), timeout=10) as connection:
        connection.settimeout(10)
        print(send_message(connection, f"REGISTER|{name}"))
        while True:
            print(send_message(connection, system_message(name)))
            print(send_message(connection, f"HEARTBEAT|{name}"))
            time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitoring client for Main.py")
    parser.add_argument("name", help="Client name, for example PC01")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--interval", type=int, default=5)
    arguments = parser.parse_args()
    run(arguments.name, arguments.host, arguments.port, arguments.interval)
