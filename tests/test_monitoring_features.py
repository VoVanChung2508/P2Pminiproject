from __future__ import annotations

import copy
import io
import json
import os
import socket
import threading
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from client import monitoring_client, process_monitor, tcp_client
from common import database
from server import server
from server import server_gui


def open_tcp_session() -> tuple[socket.socket, socket.socket, threading.Thread]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(2)
        client_socket.connect(listener.getsockname())
        accepted_socket, address = listener.accept()

    handler = threading.Thread(
        target=server.tcp_client_session,
        args=(accepted_socket, address),
    )
    handler.start()
    return accepted_socket, client_socket, handler


def exchange_tcp_message(client_socket: socket.socket, message: str) -> str:
    client_socket.sendall((message + "\n").encode("utf-8"))
    return client_socket.recv(4096).decode("utf-8").strip()


class ServerDisconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_admin_token = server.ADMIN_TOKEN
        self.previous_clients = dict(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)
        server.ADMIN_TOKEN = "test-admin-token"
        server.clients.clear()
        server.disconnected_clients.clear()
        self.register_patch = patch.object(server.db_manager, "register_client", return_value=True)
        self.status_patch = patch.object(server.db_manager, "update_status", return_value=True)
        self.metrics_patch = patch.object(server.db_manager, "update_metrics", return_value=True)
        self.alert_patch = patch.object(server.db_manager, "add_alert", return_value=True)
        self.heartbeat_patch = patch.object(server.db_manager, "update_heartbeat", return_value=True)
        for active_patch in (
            self.register_patch,
            self.status_patch,
            self.metrics_patch,
            self.alert_patch,
            self.heartbeat_patch,
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.client = server.app.test_client()
        server.register_client("node-01", "127.0.0.1")

    def tearDown(self) -> None:
        server.ADMIN_TOKEN = self.previous_admin_token
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)

    def test_disconnect_requires_admin_token(self) -> None:
        response = self.client.post("/api/clients/node-01/disconnect")
        invalid_response = self.client.post(
            "/api/clients/node-01/disconnect",
            headers={"X-Admin-Token": "wrong-token"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(invalid_response.status_code, 401)
        self.assertEqual(server.clients["node-01"]["status"], "ONLINE")

    def test_admin_disconnect_blocks_client_until_registers_again(self) -> None:
        response = self.client.post(
            "/api/clients/node-01/disconnect",
            headers={"X-Admin-Token": "test-admin-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.clients["node-01"]["status"], "OFFLINE")
        self.assertEqual(server.handle_message("HEARTBEAT|node-01", ("127.0.0.1", 1)), "ERROR|DISCONNECTED")
        self.assertEqual(
            server.handle_message("SYSTEM|node-01|cpu=1", ("127.0.0.1", 1)),
            "ERROR|DISCONNECTED",
        )

        self.assertEqual(server.handle_message("REGISTER|node-01", ("127.0.0.1", 1)), "OK|REGISTERED")
        self.assertEqual(server.handle_message("HEARTBEAT|node-01", ("127.0.0.1", 1)), "OK|HEARTBEAT")

    def test_missing_admin_configuration_disables_disconnect(self) -> None:
        server.ADMIN_TOKEN = ""

        response = self.client.post(
            "/api/clients/node-01/disconnect",
            headers={"X-Admin-Token": "test-admin-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(server.clients["node-01"]["status"], "ONLINE")

    def test_dashboard_renders_disconnect_control(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"disconnectClient", response.data)
        self.assertIn(b"const adminDisconnectEnabled = true", response.data)

    def test_dashboard_renders_last_seen_column_and_missing_value_fallback(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<th>Last Seen</th>", response.data)
        self.assertIn(b"colspan=\"9\"", response.data)
        self.assertIn(b"renderLastSeen(c.last_seen)", response.data)
        self.assertIn(b"value == null || value === '' ? ", response.data)
        self.assertIn(b"escapeHtml(value)", response.data)
        self.assertIn(b"escapeHtml(c.status)", response.data)


class TCPClientSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_clients = {
            key: dict(value) for key, value in server.clients.items()
        }
        self.previous_disconnected = set(server.disconnected_clients)
        server.clients.clear()
        server.disconnected_clients.clear()

        self.database_patches = (
            patch.object(server.db_manager, "register_client", return_value=True),
            patch.object(server.db_manager, "update_heartbeat", return_value=True),
            patch.object(server.db_manager, "update_status", return_value=True),
            patch.object(server.db_manager, "update_metrics", return_value=True),
            patch.object(server.db_manager, "add_alert", return_value=True),
        )
        for active_patch in self.database_patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def tearDown(self) -> None:
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)

    def open_session(
        self,
    ) -> tuple[socket.socket, socket.socket, threading.Thread]:
        return open_tcp_session()

    def exchange(self, client_socket: socket.socket, message: str) -> str:
        return exchange_tcp_message(client_socket, message)

    def test_register_heartbeat_and_logout_use_existing_protocol(self) -> None:
        accepted_socket, client_socket, handler = self.open_session()
        self.addCleanup(client_socket.close)

        self.assertEqual(
            self.exchange(client_socket, "REGISTER|node-01|127.0.0.1|8888"),
            "OK|REGISTERED",
        )
        self.assertEqual(
            self.exchange(client_socket, "HEARTBEAT|node-01"),
            "OK|HEARTBEAT",
        )
        self.assertEqual(
            self.exchange(
                client_socket,
                "SYSTEM|node-01|CPU=12|RAM=34|DISK=56|NETWORK=7",
            ),
            "OK|SYSTEM",
        )
        self.assertEqual(
            self.exchange(client_socket, "LOGOUT|node-01"),
            "OK|LOGOUT",
        )

        client_socket.close()
        handler.join(timeout=2)
        self.assertFalse(handler.is_alive())
        self.assertEqual(accepted_socket.fileno(), -1)
        self.assertEqual(server.clients["node-01"]["status"], "OFFLINE")

    def test_normal_eof_closes_handler_without_marking_client_offline(self) -> None:
        accepted_socket, client_socket, handler = self.open_session()

        self.assertEqual(
            self.exchange(client_socket, "REGISTER|node-02"),
            "OK|REGISTERED",
        )
        client_socket.close()
        handler.join(timeout=2)

        self.assertFalse(handler.is_alive())
        self.assertEqual(accepted_socket.fileno(), -1)
        self.assertEqual(server.clients["node-02"]["status"], "ONLINE")
        server.db_manager.update_status.assert_not_called()

    def test_idle_socket_timeout_terminates_and_closes_handler(self) -> None:
        accepted_socket, client_socket = socket.socketpair()
        handler = threading.Thread(
            target=server.tcp_client_session,
            args=(accepted_socket, ("127.0.0.1", 12346)),
        )
        with patch.object(server, "TCP_CLIENT_TIMEOUT", 0.05):
            with self.assertLogs(server.logger, level="WARNING") as captured:
                handler.start()
                handler.join(timeout=2)

        self.assertFalse(handler.is_alive())
        self.assertEqual(accepted_socket.fileno(), -1)
        self.assertTrue(any("timed out" in entry for entry in captured.output))
        client_socket.close()

    def test_connection_reset_terminates_and_closes_handler(self) -> None:
        connection = Mock()
        connection.recv.side_effect = ConnectionResetError("reset")

        with self.assertLogs(server.logger, level="INFO") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12347))

        connection.close.assert_called_once_with()
        self.assertTrue(any("reset connection" in entry for entry in captured.output))

    def test_broken_pipe_terminates_and_closes_handler(self) -> None:
        connection = Mock()
        connection.recv.return_value = b"REGISTER|node-03\n"
        connection.sendall.side_effect = BrokenPipeError("closed")

        with self.assertLogs(server.logger, level="INFO") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12348))

        connection.close.assert_called_once_with()
        self.assertTrue(any("closed before response" in entry for entry in captured.output))

    def test_unexpected_socket_error_is_logged_and_connection_is_closed(self) -> None:
        connection = Mock()
        connection.recv.side_effect = OSError("socket failure")

        with self.assertLogs(server.logger, level="ERROR") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12349))

        connection.close.assert_called_once_with()
        self.assertTrue(any("TCP receive failed" in entry for entry in captured.output))

    def test_oversized_incomplete_tcp_frame_is_logged_and_connection_closed(self) -> None:
        connection = Mock()
        connection.recv.return_value = b"x" * (
            server.PROCESS_LIST_MAX_TCP_FRAME_BYTES + 1
        )

        with self.assertLogs(server.logger, level="WARNING") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12351))

        connection.close.assert_called_once_with()
        self.assertTrue(
            any("exceeded maximum size" in entry for entry in captured.output)
        )

    def test_unexpected_send_error_is_logged_and_connection_is_closed(self) -> None:
        connection = Mock()
        connection.recv.return_value = b"REGISTER|node-04\n"
        connection.sendall.side_effect = OSError("send failure")

        with self.assertLogs(server.logger, level="ERROR") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12350))

        connection.close.assert_called_once_with()
        self.assertTrue(any("TCP send failed" in entry for entry in captured.output))

    def test_multiple_clients_connect_concurrently(self) -> None:
        sessions = [self.open_session() for _ in range(4)]
        try:
            senders = [
                threading.Thread(
                    target=client_socket.sendall,
                    args=((f"REGISTER|parallel-{index}\n").encode("utf-8"),),
                )
                for index, (_, client_socket, _) in enumerate(sessions)
            ]
            for sender in senders:
                sender.start()
            for sender in senders:
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive())

            for index, (_, client_socket, _) in enumerate(sessions):
                self.assertEqual(
                    client_socket.recv(4096).decode("utf-8").strip(),
                    "OK|REGISTERED",
                )
                client_socket.close()
            for accepted_socket, _, handler in sessions:
                handler.join(timeout=2)
                self.assertFalse(handler.is_alive())
                self.assertEqual(accepted_socket.fileno(), -1)
            self.assertTrue(
                all(f"parallel-{index}" in server.clients for index in range(4))
            )
        finally:
            for _, client_socket, _ in sessions:
                client_socket.close()
            for _, _, handler in sessions:
                handler.join(timeout=2)

    def test_concurrent_clients_keep_protocol_state_isolated(self) -> None:
        for count in (2, 5, 10):
            prefix = f"multi-{count}-{uuid.uuid4().hex[:8]}"
            names = [f"{prefix}-{index}" for index in range(count)]
            metrics = [
                {
                    "cpu": float(10 + index),
                    "ram": float(20 + index),
                    "disk": float(30 + index),
                    "network": float(40 + index),
                }
                for index in range(count)
            ]
            sessions = [self.open_session() for _ in names]
            try:
                def broadcast(message_for_index: Any) -> list[str]:
                    with ThreadPoolExecutor(max_workers=count) as executor:
                        futures = [
                            executor.submit(
                                self.exchange,
                                client_socket,
                                message_for_index(index),
                            )
                            for index, (_, client_socket, _) in enumerate(sessions)
                        ]
                        return [future.result(timeout=10) for future in futures]

                register_responses = broadcast(
                    lambda index: f"REGISTER|{names[index]}|127.0.0.1|8888"
                )
                self.assertEqual(register_responses, ["OK|REGISTERED"] * count)

                system_responses = broadcast(
                    lambda index: "SYSTEM|{}|CPU={}|RAM={}|DISK={}|NETWORK={}".format(
                        names[index],
                        metrics[index]["cpu"],
                        metrics[index]["ram"],
                        metrics[index]["disk"],
                        metrics[index]["network"],
                    )
                )
                self.assertEqual(system_responses, ["OK|SYSTEM"] * count)

                heartbeat_responses = broadcast(
                    lambda index: f"HEARTBEAT|{names[index]}"
                )
                self.assertEqual(heartbeat_responses, ["OK|HEARTBEAT"] * count)

                runtime_clients = {
                    key: server.clients[key.lower()] for key in names
                }
                self.assertEqual(len(runtime_clients), count)
                for client in runtime_clients.values():
                    self.assertEqual(client["status"], "ONLINE")
                    self.assertIsInstance(client["last_seen_epoch"], float)
                    self.assertGreater(client["last_seen_epoch"], 0)

                metric_calls = {
                    call.args[0]: call.args[1]
                    for call in server.db_manager.update_metrics.call_args_list
                    if call.args[0] in names
                }
                self.assertEqual(
                    {name: metric_calls[name] for name in names},
                    {name: metrics[index] for index, name in enumerate(names)},
                )
                self.assertCountEqual(
                    [
                        call.args[0]
                        for call in server.db_manager.register_client.call_args_list
                        if call.args[0] in names
                    ],
                    names,
                )
                self.assertCountEqual(
                    [
                        call.args[0]
                        for call in server.db_manager.update_heartbeat.call_args_list
                        if call.args[0] in names
                    ],
                    names,
                )

                self.assertEqual(
                    self.exchange(sessions[0][1], f"LOGOUT|{names[0]}"),
                    "OK|LOGOUT",
                )
                self.assertEqual(server.clients[names[0]]["status"], "OFFLINE")
                for name in names[1:]:
                    self.assertEqual(server.clients[name]["status"], "ONLINE")

                if count > 1:
                    sessions[1][1].close()
                    sessions[1][2].join(timeout=2)
                    self.assertFalse(sessions[1][2].is_alive())
                    self.assertEqual(server.clients[names[1]]["status"], "ONLINE")
                    for name in names[2:]:
                        self.assertEqual(server.clients[name]["status"], "ONLINE")
            finally:
                for _, client_socket, _ in sessions:
                    client_socket.close()
                for _, _, handler in sessions:
                    handler.join(timeout=2)
                    self.assertFalse(handler.is_alive())

    def test_accept_error_is_logged_and_stops_listener_loop(self) -> None:
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.fileno.return_value = 10
        listener.accept.side_effect = OSError("accept failed")

        with patch.object(server.socket, "socket", return_value=listener):
            with self.assertLogs(server.logger, level="ERROR") as captured:
                server.tcp_server()

        listener.__exit__.assert_called_once()
        self.assertTrue(any("TCP accept failed" in entry for entry in captured.output))


class ProcessMonitoringTests(unittest.TestCase):
    SAMPLE_PROCESS = {
        "pid": 123,
        "name": "worker",
        "username": "monitor",
        "cpu_percent": 12.5,
        "memory_percent": 3.25,
        "status": "running",
    }
    SECOND_PROCESS = {
        "pid": 456,
        "name": "worker-helper",
        "username": None,
        "cpu_percent": 2.0,
        "memory_percent": 1.0,
        "status": "sleeping",
    }

    def setUp(self) -> None:
        self.previous_admin_token = server.ADMIN_TOKEN
        self.previous_clients = copy.deepcopy(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)
        self.previous_process_requests = copy.deepcopy(server.process_requests)
        server.ADMIN_TOKEN = "test-process-admin-token"
        server.clients.clear()
        server.disconnected_clients.clear()
        server.process_requests.clear()

        self.database_patches = (
            patch.object(server.db_manager, "register_client", return_value=True),
            patch.object(server.db_manager, "update_heartbeat", return_value=True),
            patch.object(server.db_manager, "update_status", return_value=True),
        )
        for active_patch in self.database_patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        server.ADMIN_TOKEN = self.previous_admin_token
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)
        server.process_requests.clear()
        server.process_requests.update(self.previous_process_requests)

    def register(self, name: str, *, capable: bool = True) -> None:
        capability = f"|{server.PROCESS_LIST_CAPABILITY}" if capable else ""
        response = server.handle_message(
            f"REGISTER|{name}|127.0.0.1|8888{capability}",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "OK|REGISTERED")

    def admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": server.ADMIN_TOKEN}

    def request_process_list(self, name: str):
        return self.client.post(
            f"/api/clients/{name}/process-list",
            headers=self.admin_headers(),
        )

    def test_admin_request_is_delivered_and_structured_result_is_retrieved(self) -> None:
        name = "process-node"
        self.register(name)

        queued = self.request_process_list(name)
        self.assertEqual(queued.status_code, 202)
        request_id = queued.get_json()["request"]["request_id"]

        command = server.handle_message(
            f"HEARTBEAT|{name}",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(
            command,
            f"COMMAND|GET_PROCESS_LIST|{request_id}|{server.PROCESS_LIST_TOP_N}",
        )
        reply = server.handle_message(
            f"PROCESS_LIST|{name}|{request_id}|"
            + json.dumps([self.SAMPLE_PROCESS, self.SECOND_PROCESS]),
            ("127.0.0.1", 30000),
        )
        self.assertEqual(reply, "OK|PROCESS_LIST")

        result = self.client.get(
            f"/api/clients/{name}/process-list",
            headers=self.admin_headers(),
        )
        request_data = result.get_json()["request"]
        self.assertEqual(request_data["status"], "complete")
        self.assertEqual(
            request_data["processes"],
            [self.SAMPLE_PROCESS, self.SECOND_PROCESS],
        )

    def test_request_and_result_routes_require_admin_token(self) -> None:
        self.register("process-node")

        response = self.client.post("/api/clients/process-node/process-list")
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            "/api/clients/process-node/process-list",
            headers={"X-Admin-Token": "incorrect"},
        )
        self.assertEqual(response.status_code, 401)

    def test_legacy_client_does_not_receive_process_commands(self) -> None:
        self.register("legacy-node", capable=False)

        response = self.request_process_list("legacy-node")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            server.handle_message(
                "HEARTBEAT|legacy-node",
                ("127.0.0.1", 30000),
            ),
            "OK|HEARTBEAT",
        )

    def test_unregistered_client_cannot_be_requested(self) -> None:
        response = self.request_process_list("unknown-node")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(server.process_requests, {})

    def test_malformed_process_reply_is_rejected_and_reported(self) -> None:
        name = "malformed-node"
        self.register(name)
        request_id = self.request_process_list(name).get_json()["request"]["request_id"]
        server.handle_message(f"HEARTBEAT|{name}", ("127.0.0.1", 30000))

        response = server.handle_message(
            f"PROCESS_LIST|{name}|{request_id}|"
            + json.dumps([{"pid": 1, "name": "incomplete"}]),
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "ERROR|MALFORMED_PROCESS_LIST")
        self.assertEqual(server.process_requests[name]["status"], "error")

    def test_process_reply_from_different_tcp_peer_is_rejected(self) -> None:
        name = "spoofed-node"
        self.register(name)
        request_id = self.request_process_list(name).get_json()["request"]["request_id"]
        server.handle_message(
            f"HEARTBEAT|{name}",
            ("127.0.0.1", 30000),
        )

        response = server.handle_message(
            f"PROCESS_LIST|{name}|{request_id}|{json.dumps([self.SAMPLE_PROCESS])}",
            ("127.0.0.1", 30001),
        )

        self.assertEqual(response, "ERROR|INVALID_PROCESS_LIST_REQUEST")
        self.assertEqual(server.process_requests[name]["status"], "delivered")

    def test_process_reply_is_capped_and_payload_size_is_bounded(self) -> None:
        name = "oversized-node"
        self.register(name)
        request_id = self.request_process_list(name).get_json()["request"]["request_id"]
        server.handle_message(f"HEARTBEAT|{name}", ("127.0.0.1", 30000))
        too_many = [
            {**self.SAMPLE_PROCESS, "pid": index + 1}
            for index in range(server.PROCESS_LIST_TOP_N + 1)
        ]

        response = server.handle_message(
            f"PROCESS_LIST|{name}|{request_id}|{json.dumps(too_many)}",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "ERROR|MALFORMED_PROCESS_LIST")

        request_id = self.request_process_list(name).get_json()["request"]["request_id"]
        server.handle_message(f"HEARTBEAT|{name}", ("127.0.0.1", 30000))
        oversized_payload = json.dumps(
            [{**self.SAMPLE_PROCESS, "name": "x" * server.PROCESS_LIST_MAX_PAYLOAD_BYTES}]
        )
        response = server.handle_message(
            f"PROCESS_LIST|{name}|{request_id}|{oversized_payload}",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "ERROR|MALFORMED_PROCESS_LIST")

    def test_client_unavailable_process_data_is_reported_to_server(self) -> None:
        name = "unavailable-node"
        self.register(name)
        request_id = self.request_process_list(name).get_json()["request"]["request_id"]
        server.handle_message(f"HEARTBEAT|{name}", ("127.0.0.1", 30000))

        response = server.handle_message(
            f"PROCESS_LIST_ERROR|{name}|{request_id}|UNAVAILABLE",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "OK|PROCESS_LIST")
        self.assertEqual(server.process_requests[name]["status"], "error")

    def test_expired_request_is_not_delivered(self) -> None:
        name = "expired-node"
        self.register(name)
        queued = self.request_process_list(name)
        self.assertEqual(queued.status_code, 202)
        request_id = queued.get_json()["request"]["request_id"]
        expiry = server.process_requests[name]["expires_at"]

        with patch("server.server.time.time", return_value=expiry + 1):
            response = server.handle_message(
                f"HEARTBEAT|{name}",
                ("127.0.0.1", 30000),
            )
        self.assertEqual(response, "OK|HEARTBEAT")
        self.assertEqual(server.process_requests[name]["status"], "timeout")
        self.assertNotIn(request_id, response)

    def test_client_answers_only_allowlisted_process_list_command(self) -> None:
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=None)
        fake_socket.recv.side_effect = [
            b"COMMAND|GET_PROCESS_LIST|abc123|20\n",
            b"OK|PROCESS_LIST\n",
        ]
        with patch.object(
            tcp_client.socket, "create_connection", return_value=fake_socket
        ):
            with patch.object(
                tcp_client,
                "collect_process_list",
                return_value=[self.SAMPLE_PROCESS],
            ) as collect:
                result = tcp_client.TCPClient(
                    host="127.0.0.1",
                    port=8888,
                    default_name="process-node",
                ).heartbeat()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["raw"], "OK|HEARTBEAT")
        self.assertEqual(result["process_list_status"], "complete")
        collect.assert_called_once_with(20)
        sent_lines = [
            call.args[0].decode("utf-8").strip()
            for call in fake_socket.sendall.call_args_list
        ]
        self.assertEqual(sent_lines[0], "HEARTBEAT|process-node")
        self.assertTrue(sent_lines[1].startswith("PROCESS_LIST|process-node|abc123|"))
        self.assertEqual(json.loads(sent_lines[1].split("|", 3)[3]), [self.SAMPLE_PROCESS])

    def test_client_registration_advertises_fixed_process_capability(self) -> None:
        message = tcp_client.TCPClient(default_name="process-node")._build_message(
            "REGISTER",
            {
                "name": "process-node",
                "host": "127.0.0.1",
                "port": 8888,
            },
        )

        self.assertEqual(
            message,
            "REGISTER|process-node|127.0.0.1|8888|PROCESS_LIST_V1",
        )
        self.assertEqual(
            server.handle_message(message, ("127.0.0.1", 30000)),
            "OK|REGISTERED",
        )
        self.assertTrue(server.clients["process-node"]["process_list_capable"])

    def test_raw_socket_registration_fallback_advertises_capability(self) -> None:
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=None)
        fake_socket.recv.return_value = b"OK|REGISTERED\n"
        with patch.object(monitoring_client, "TCPClient", None):
            with patch.object(monitoring_client, "HTTPClient", None):
                with patch.object(
                    monitoring_client.socket,
                    "create_connection",
                    return_value=fake_socket,
                ):
                    client = monitoring_client.NetworkMonitoringClient(
                        name="fallback-node"
                    )
                    result = client.register()

        self.assertEqual(result["status"], "ok")
        fake_socket.sendall.assert_called_once_with(
            b"REGISTER|fallback-node|127.0.0.1|8888|PROCESS_LIST_V1\n"
        )

    def test_process_request_completes_over_real_loopback_tcp_exchange(self) -> None:
        stop_listener = threading.Event()
        handler_threads: list[threading.Thread] = []
        listener_errors: list[OSError] = []
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.1)
        tcp_port = listener.getsockname()[1]

        def accept_connections() -> None:
            while not stop_listener.is_set():
                try:
                    connection, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError as error:
                    if not stop_listener.is_set():
                        listener_errors.append(error)
                    return
                handler = threading.Thread(
                    target=server.tcp_client_session,
                    args=(connection, address),
                )
                handler_threads.append(handler)
                handler.start()

        accept_thread = threading.Thread(target=accept_connections)
        accept_thread.start()
        name = "loopback-process-node"
        tcp_peer = tcp_client.TCPClient(
            host="127.0.0.1",
            port=tcp_port,
            default_name=name,
        )
        try:
            self.assertEqual(tcp_peer.register()["status"], "ok")
            ordinary_heartbeat = tcp_peer.heartbeat()
            self.assertEqual(ordinary_heartbeat["raw"], "OK|HEARTBEAT")
            self.assertNotIn("process_list_status", ordinary_heartbeat)
            queued = self.request_process_list(name)
            self.assertEqual(queued.status_code, 202)

            with patch.object(
                tcp_client,
                "collect_process_list",
                return_value=[self.SAMPLE_PROCESS, self.SECOND_PROCESS],
            ):
                heartbeat = tcp_peer.heartbeat()
            self.assertEqual(heartbeat["status"], "ok")
            self.assertEqual(heartbeat["process_list_status"], "complete")

            result = self.client.get(
                f"/api/clients/{name}/process-list",
                headers=self.admin_headers(),
            )
            self.assertEqual(result.get_json()["request"]["status"], "complete")
            self.assertEqual(
                result.get_json()["request"]["processes"],
                [self.SAMPLE_PROCESS, self.SECOND_PROCESS],
            )
        finally:
            stop_listener.set()
            listener.close()
            accept_thread.join(timeout=2)
            self.assertFalse(accept_thread.is_alive())
            for handler in handler_threads:
                handler.join(timeout=2)
                self.assertFalse(handler.is_alive())
            self.assertEqual(listener_errors, [])

    def test_client_does_not_execute_arbitrary_server_command(self) -> None:
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=None)
        fake_socket.recv.return_value = b"COMMAND|RUN_SHELL|abc123|whoami\n"

        with patch.object(
            tcp_client.socket, "create_connection", return_value=fake_socket
        ):
            with patch.object(tcp_client, "collect_process_list") as collect:
                result = tcp_client.TCPClient(default_name="process-node").heartbeat()

        self.assertEqual(result["status"], "error")
        collect.assert_not_called()
        fake_socket.sendall.assert_called_once_with(b"HEARTBEAT|process-node\n")

    def test_client_rejects_malformed_process_list_command(self) -> None:
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=None)
        fake_socket.recv.return_value = b"COMMAND|GET_PROCESS_LIST|abc123|999\n"

        with patch.object(
            tcp_client.socket, "create_connection", return_value=fake_socket
        ):
            with patch.object(tcp_client, "collect_process_list") as collect:
                result = tcp_client.TCPClient(default_name="process-node").heartbeat()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "Unsupported server command")
        collect.assert_not_called()
        fake_socket.sendall.assert_called_once_with(b"HEARTBEAT|process-node\n")

    def test_client_handles_server_timeout_and_disconnect_during_collection(self) -> None:
        timed_out_socket = Mock()
        timed_out_socket.__enter__ = Mock(return_value=timed_out_socket)
        timed_out_socket.__exit__ = Mock(return_value=None)
        timed_out_socket.recv.side_effect = socket.timeout("timed out")
        with patch.object(
            tcp_client.socket, "create_connection", return_value=timed_out_socket
        ):
            timeout_result = tcp_client.TCPClient(default_name="node").heartbeat()
        self.assertEqual(timeout_result["status"], "error")

        disconnected_socket = Mock()
        disconnected_socket.__enter__ = Mock(return_value=disconnected_socket)
        disconnected_socket.__exit__ = Mock(return_value=None)
        disconnected_socket.recv.side_effect = [
            b"COMMAND|GET_PROCESS_LIST|abc123|20\n",
            b"",
        ]
        with patch.object(
            tcp_client.socket,
            "create_connection",
            return_value=disconnected_socket,
        ):
            with patch.object(
                tcp_client,
                "collect_process_list",
                return_value=[self.SAMPLE_PROCESS],
            ):
                disconnect_result = tcp_client.TCPClient(
                    default_name="process-node"
                ).heartbeat()
        self.assertEqual(disconnect_result["status"], "error")
        self.assertEqual(
            disconnect_result["message"],
            "No acknowledgement for process-list response",
        )

    def test_unknown_inbound_command_is_rejected(self) -> None:
        response = server.handle_message(
            "RUN_SHELL|whoami",
            ("127.0.0.1", 30000),
        )

        self.assertEqual(response, "ERROR|Unsupported message")


class ProcessCollectorTests(unittest.TestCase):
    def test_process_collector_caps_and_handles_disappeared_or_denied_processes(self) -> None:
        class FakeError(Exception):
            pass

        class FakeNoSuchProcess(FakeError):
            pass

        class FakeZombieProcess(FakeNoSuchProcess):
            pass

        class FakeAccessDenied(FakeError):
            pass

        class FakePsutil:
            Error = FakeError
            NoSuchProcess = FakeNoSuchProcess
            ZombieProcess = FakeZombieProcess
            AccessDenied = FakeAccessDenied

            class Process:
                def __init__(self, info=None, error=None):
                    self._info = info
                    self._error = error

                @property
                def info(self):
                    if self._error:
                        raise self._error
                    return self._info

            @staticmethod
            def process_iter(*, attrs, ad_value):
                self.assertIn("pid", attrs)
                self.assertIsNone(ad_value)
                processes = [
                    FakePsutil.Process(
                        info={
                            "pid": index + 1,
                            "name": f"process-{index}",
                            "username": None,
                            "cpu_percent": float(index % 101),
                            "memory_percent": float(index % 101),
                            "status": "running",
                        }
                    )
                    for index in range(25)
                ]
                denied = FakePsutil.Process(error=FakeAccessDenied())
                missing = FakePsutil.Process(error=FakeNoSuchProcess())
                return [*processes, denied, missing]

        with patch.object(process_monitor, "psutil", FakePsutil):
            result = process_monitor.collect_process_list()

        self.assertEqual(len(result), process_monitor.TOP_N)
        self.assertTrue(
            all(set(process) == process_monitor.PROCESS_FIELDS for process in result)
        )
        self.assertEqual(
            [process["memory_percent"] for process in result],
            sorted(
                (process["memory_percent"] for process in result),
                reverse=True,
            ),
        )

    def test_process_collector_rejects_unbounded_or_unavailable_requests(self) -> None:
        with self.assertRaises(ValueError):
            process_monitor.collect_process_list(process_monitor.TOP_N + 1)
        with patch.object(process_monitor, "psutil", None):
            with self.assertRaises(RuntimeError):
                process_monitor.collect_process_list()


@unittest.skipUnless(
    os.environ.get("MYSQL_INTEGRATION_TEST") == "1"
    and os.environ.get("MYSQL_INTEGRATION_TEST_DB"),
    "Set MYSQL_INTEGRATION_TEST=1 and MYSQL_INTEGRATION_TEST_DB to enable "
    "multi-client MySQL integration coverage.",
)
class MySQLMultiClientIntegrationTests(unittest.TestCase):
    def test_concurrent_tcp_clients_persist_independent_records(self) -> None:
        if not database.MYSQL_AVAILABLE:
            self.skipTest("mysql-connector-python is unavailable.")

        manager = database.DatabaseManager(
            database=os.environ["MYSQL_INTEGRATION_TEST_DB"],
            create_database=False,
        )
        if not manager.connect():
            manager.close()
            self.skipTest("Configured MySQL integration database is unavailable.")

        previous_clients = {
            key: dict(value) for key, value in server.clients.items()
        }
        previous_disconnected = set(server.disconnected_clients)
        server.clients.clear()
        server.disconnected_clients.clear()

        run_id = uuid.uuid4().hex
        test_names: list[str] = []
        sessions: list[
            tuple[socket.socket, socket.socket, threading.Thread]
        ] = []
        try:
            with patch.object(server, "db_manager", manager):
                for count in (2, 5, 10):
                    names = [
                        f"mysql-multi-{run_id}-{count}-{index}"
                        for index in range(count)
                    ]
                    test_names.extend(name.lower() for name in names)
                    metrics = [
                        {
                            "cpu": float(10 + index),
                            "ram": float(20 + index),
                            "disk": float(30 + index),
                            "network": float(40 + index),
                        }
                        for index in range(count)
                    ]
                    batch_sessions = [open_tcp_session() for _ in names]
                    sessions.extend(batch_sessions)

                    def run_client(index: int) -> list[str]:
                        client_socket = batch_sessions[index][1]
                        metric = metrics[index]
                        return [
                            exchange_tcp_message(
                                client_socket,
                                f"REGISTER|{names[index]}|127.0.0.1|8888",
                            ),
                            exchange_tcp_message(
                                client_socket,
                                "SYSTEM|{}|CPU={}|RAM={}|DISK={}|NETWORK={}".format(
                                    names[index],
                                    metric["cpu"],
                                    metric["ram"],
                                    metric["disk"],
                                    metric["network"],
                                ),
                            ),
                            exchange_tcp_message(
                                client_socket, f"HEARTBEAT|{names[index]}"
                            ),
                        ]

                    with ThreadPoolExecutor(max_workers=count) as executor:
                        results = list(
                            executor.map(run_client, range(count), timeout=30)
                        )
                    self.assertEqual(
                        results,
                        [
                            ["OK|REGISTERED", "OK|SYSTEM", "OK|HEARTBEAT"]
                            for _ in names
                        ],
                    )

                    self.assertEqual(
                        exchange_tcp_message(
                            batch_sessions[0][1], f"LOGOUT|{names[0]}"
                        ),
                        "OK|LOGOUT",
                    )
                    batch_sessions[0][1].close()
                    batch_sessions[0][2].join(timeout=2)
                    self.assertFalse(batch_sessions[0][2].is_alive())
                    self.assertEqual(
                        server.clients[names[0].lower()]["status"], "OFFLINE"
                    )

                    if count > 1:
                        batch_sessions[1][1].close()
                        batch_sessions[1][2].join(timeout=2)
                        self.assertFalse(batch_sessions[1][2].is_alive())
                        self.assertEqual(
                            server.clients[names[1].lower()]["status"], "ONLINE"
                        )

                    for name in names[2:]:
                        self.assertEqual(
                            server.clients[name.lower()]["status"], "ONLINE"
                        )

                    persisted_clients = {
                        row["name"]: row
                        for row in manager.get_clients()
                        if row["name"] in names
                    }
                    self.assertEqual(set(persisted_clients), set(names))
                    self.assertEqual(len(persisted_clients), count)
                    for index, name in enumerate(names):
                        record = persisted_clients[name]
                        expected_status = "OFFLINE" if index == 0 else "ONLINE"
                        self.assertEqual(record["status"], expected_status)
                        self.assertIsNotNone(record["last_seen"])
                        for metric_name, expected_value in metrics[index].items():
                            self.assertEqual(record[metric_name], expected_value)

                        history = manager.get_history(name)
                        self.assertEqual(len(history), 1)
                        for metric_name, expected_value in metrics[index].items():
                            self.assertEqual(history[0][metric_name], expected_value)
        finally:
            for _, client_socket, _ in sessions:
                client_socket.close()
            for _, _, handler in sessions:
                handler.join(timeout=2)
                self.assertFalse(handler.is_alive())

            try:
                if test_names and manager.is_connected and manager.db_conn is not None:
                    placeholders = ", ".join(["%s"] * len(test_names))
                    cursor = manager.db_conn.cursor()
                    try:
                        for table in ("history", "alerts", "clients"):
                            cursor.execute(
                                f"DELETE FROM {table} "
                                f"WHERE client_key IN ({placeholders})",
                                tuple(test_names),
                            )
                        manager.db_conn.commit()
                    except Exception:
                        manager.db_conn.rollback()
                        raise
                    finally:
                        cursor.close()
            finally:
                server.clients.clear()
                server.clients.update(previous_clients)
                server.disconnected_clients.clear()
                server.disconnected_clients.update(previous_disconnected)
                manager.close()


class DatabaseStrictStorageTests(unittest.TestCase):
    def test_unavailable_mysql_does_not_report_writes_as_saved(self) -> None:
        manager = database.DatabaseManager()

        with patch.object(database, "MYSQL_AVAILABLE", False):
            self.assertFalse(manager.connect())

        self.assertFalse(manager.register_client("node-01", "127.0.0.1"))
        self.assertFalse(manager.update_metrics("node-01", {"cpu": 10.0}))
        self.assertFalse(manager.add_alert("node-01", "cpu", 90.0, 80.0))
        self.assertFalse(manager.update_status("node-01", "OFFLINE"))

    def test_mysql_connection_uses_configured_credentials(self) -> None:
        manager = database.DatabaseManager(
            host="db.example",
            user="monitor",
            password="unit-test-password",
            database="monitoring",
            port=3307,
        )
        server_connection = Mock()
        database_connection = Mock()

        with patch.object(
            database.mysql.connector,
            "connect",
            side_effect=[server_connection, database_connection],
        ) as connect:
            with patch.object(manager, "_create_tables"):
                self.assertTrue(manager.connect())

        self.assertEqual(connect.call_args_list[0].kwargs["password"], "unit-test-password")
        self.assertEqual(connect.call_args_list[1].kwargs["database"], "monitoring")
        self.assertTrue(manager.is_connected)
        manager.close()

    def test_connect_to_existing_workbench_database_without_create_privilege(self) -> None:
        manager = database.DatabaseManager(
            host="127.0.0.1",
            user="monitor_user",
            password="secret",
            database="network_monitor",
            port=3306,
            create_database=False,
        )
        existing_database_connection = Mock()

        with patch.object(
            database.mysql.connector,
            "connect",
            return_value=existing_database_connection,
        ) as connect:
            with patch.object(manager, "_create_tables"):
                self.assertTrue(manager.connect())

        connect.assert_called_once_with(
            host="127.0.0.1",
            user="monitor_user",
            password="secret",
            database="network_monitor",
            port=3306,
            autocommit=False,
        )
        self.assertTrue(manager.is_connected)
        manager.close()

    def test_explicit_empty_mysql_password_does_not_use_environment_password(self) -> None:
        with patch.dict(os.environ, {"MYSQL_PASSWORD": "environment-password"}):
            manager = database.DatabaseManager(password="")

        self.assertEqual(manager.password, "")

    def test_mysql_connection_uses_environment_variables(self) -> None:
        configuration = {
            "MYSQL_HOST": "mysql.example",
            "MYSQL_PORT": "3307",
            "MYSQL_USER": "monitor_user",
            "MYSQL_PASSWORD": "from-environment",
            "MYSQL_DB": "workbench_schema",
            "MYSQL_CREATE_DATABASE": "false",
        }

        with patch.dict(os.environ, configuration, clear=False):
            manager = database.DatabaseManager()

        self.assertEqual(manager.host, "mysql.example")
        self.assertEqual(manager.port, 3307)
        self.assertEqual(manager.user, "monitor_user")
        self.assertEqual(manager.password, "from-environment")
        self.assertEqual(manager.database, "workbench_schema")
        self.assertFalse(manager.create_database)

    def test_dotenv_password_replaces_empty_windows_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / ".env"
            env_file.write_text("MYSQL_PASSWORD=local-file-password\n", encoding="utf-8")
            with patch.dict(os.environ, {"MYSQL_PASSWORD": ""}):
                database.load_project_environment(env_file)
                manager = database.DatabaseManager()

        self.assertEqual(manager.password, "local-file-password")

    def test_nonempty_environment_password_takes_precedence_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / ".env"
            env_file.write_text("MYSQL_PASSWORD=file-password\n", encoding="utf-8")
            with patch.dict(os.environ, {"MYSQL_PASSWORD": "windows-password"}):
                database.load_project_environment(env_file)
                manager = database.DatabaseManager()

        self.assertEqual(manager.password, "windows-password")

    def test_mysql_write_failure_marks_storage_unavailable(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        manager.db_conn.cursor.side_effect = RuntimeError("connection dropped")
        manager.is_connected = True

        self.assertFalse(manager.register_client("node-01", "127.0.0.1"))
        self.assertFalse(manager.is_connected)

    def test_client_history_and_alert_writes_commit_to_mysql(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        manager.db_conn.cursor.return_value.fetchone.return_value = (1,)
        manager.db_conn.cursor.return_value.rowcount = 1
        manager.is_connected = True

        self.assertTrue(manager.register_client("node-01", "127.0.0.1"))
        self.assertTrue(
            manager.update_metrics(
                "node-01",
                {"cpu": 12.0, "ram": 34.0, "disk": 56.0, "network": 7.0},
            )
        )
        self.assertTrue(manager.add_alert("node-01", "cpu", 90.0, 80.0))

        executed_sql = [
            call.args[0] for call in manager.db_conn.cursor.return_value.execute.call_args_list
        ]
        self.assertEqual(
            sum("INSERT INTO clients" in query for query in executed_sql),
            1,
        )
        self.assertEqual(
            sum("INSERT INTO history" in query for query in executed_sql),
            1,
        )
        self.assertEqual(
            sum("INSERT INTO alerts" in query for query in executed_sql),
            1,
        )
        self.assertEqual(manager.db_conn.commit.call_count, 3)
        self.assertEqual(manager.db_conn.rollback.call_count, 3)

    def test_health_check_reconnects_and_next_read_uses_mysql(self) -> None:
        manager = database.DatabaseManager(create_database=False)
        broken_connection = Mock()
        broken_cursor = broken_connection.cursor.return_value
        broken_cursor.execute.side_effect = database.mysql.connector.Error(
            msg="connection lost",
            errno=2013,
        )
        replacement_connection = Mock()
        replacement_cursor = replacement_connection.cursor.return_value
        replacement_cursor.fetchone.return_value = (1,)
        replacement_cursor.fetchall.return_value = []
        manager.db_conn = broken_connection
        manager.is_connected = True

        with patch.object(
            database.mysql.connector,
            "connect",
            return_value=replacement_connection,
        ) as connect:
            with patch.object(manager, "_create_tables"):
                self.assertEqual(manager.get_clients(), [])

        broken_connection.close.assert_called_once_with()
        connect.assert_called_once()
        self.assertTrue(manager.is_connected)
        broken_cursor.execute.assert_called_once_with("SELECT 1")
        self.assertIn("FROM clients", replacement_cursor.execute.call_args[0][0])

    def test_read_query_is_retried_once_after_temporary_disconnect(self) -> None:
        manager = database.DatabaseManager(create_database=False)
        broken_connection = Mock()
        broken_cursor = broken_connection.cursor.return_value
        broken_cursor.fetchone.return_value = (1,)
        broken_cursor.execute.side_effect = [
            None,
            database.mysql.connector.Error(msg="connection lost", errno=2013),
        ]
        replacement_connection = Mock()
        replacement_cursor = replacement_connection.cursor.return_value
        replacement_cursor.fetchone.return_value = (1,)
        replacement_cursor.fetchall.return_value = []
        manager.db_conn = broken_connection
        manager.is_connected = True

        with patch.object(
            database.mysql.connector,
            "connect",
            return_value=replacement_connection,
        ) as connect:
            with patch.object(manager, "_create_tables"):
                self.assertEqual(manager.get_clients(), [])

        connect.assert_called_once()
        self.assertEqual(
            [call.args[0] for call in replacement_cursor.execute.call_args_list],
            [
                "SELECT 1",
                unittest.mock.ANY,
            ],
        )
        self.assertIn("FROM clients", replacement_cursor.execute.call_args_list[1].args[0])

    def test_failed_alert_commit_is_not_replayed_after_reconnect(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        cursor = manager.db_conn.cursor.return_value
        cursor.fetchone.return_value = (1,)
        manager.db_conn.commit.side_effect = database.mysql.connector.Error(
            msg="connection lost during commit",
            errno=2013,
        )
        connection = manager.db_conn
        manager.is_connected = True

        with patch.object(manager, "connect", return_value=True) as reconnect:
            self.assertFalse(manager.add_alert("node-01", "cpu", 90.0, 80.0))

        reconnect.assert_called_once_with()
        insert_attempts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO alerts" in call.args[0]
        ]
        self.assertEqual(len(insert_attempts), 1)
        self.assertEqual(connection.rollback.call_count, 2)

    def test_failed_metrics_commit_does_not_duplicate_history(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        cursor = manager.db_conn.cursor.return_value
        cursor.fetchone.return_value = (1,)
        cursor.rowcount = 1
        manager.db_conn.commit.side_effect = database.mysql.connector.Error(
            msg="connection lost during commit",
            errno=2013,
        )
        connection = manager.db_conn
        manager.is_connected = True

        with patch.object(manager, "connect", return_value=True):
            self.assertFalse(
                manager.update_metrics(
                    "node-01",
                    {"cpu": 12.0, "ram": 34.0, "disk": 56.0, "network": 7.0},
                )
            )

        history_inserts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO history" in call.args[0]
        ]
        self.assertEqual(len(history_inserts), 1)
        self.assertEqual(connection.rollback.call_count, 2)

    def test_connection_retries_are_bounded(self) -> None:
        manager = database.DatabaseManager(create_database=False)
        unavailable = database.mysql.connector.Error(
            msg="server unavailable",
            errno=2003,
        )

        with patch.object(
            database.mysql.connector,
            "connect",
            side_effect=unavailable,
        ) as connect:
            with patch.object(database.time, "sleep") as sleep:
                with self.assertLogs(database.logger, level="ERROR") as captured:
                    self.assertFalse(manager.connect())

        self.assertEqual(connect.call_count, database.MYSQL_RECONNECT_ATTEMPTS)
        self.assertEqual(sleep.call_count, database.MYSQL_RECONNECT_ATTEMPTS - 1)
        self.assertNotIn("server unavailable", "\n".join(captured.output))

    def test_failed_query_rolls_back_transaction(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        connection = manager.db_conn
        cursor = manager.db_conn.cursor.return_value
        cursor.execute.side_effect = [
            None,
            database.mysql.connector.Error(msg="invalid query", errno=1064),
        ]
        manager.is_connected = True

        with self.assertRaises(RuntimeError):
            manager.get_clients()

        self.assertEqual(connection.rollback.call_count, 2)
        self.assertFalse(manager.is_connected)

    def test_mysql_authentication_failure_gives_configuration_guidance(self) -> None:
        manager = database.DatabaseManager(create_database=False, user="monitor")
        authentication_error = database.mysql.connector.Error(
            msg="Access denied",
            errno=1045,
        )

        with patch.object(
            database.mysql.connector,
            "connect",
            side_effect=authentication_error,
        ):
            with self.assertLogs(database.logger, level="ERROR") as captured:
                self.assertFalse(manager.connect())

        self.assertIn("MYSQL_PASSWORD", captured.output[0])
        self.assertNotIn("Access denied", captured.output[0])

    def test_unchanged_metrics_still_write_history_for_existing_client(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        cursor = manager.db_conn.cursor.return_value
        cursor.rowcount = 0
        cursor.fetchone.return_value = (1,)
        manager.is_connected = True

        self.assertTrue(
            manager.update_metrics(
                "node-01",
                {"cpu": 10.0, "ram": 20.0, "disk": 30.0, "network": 0.0},
            )
        )
        self.assertEqual(cursor.execute.call_count, 4)
        manager.db_conn.commit.assert_called_once_with()

    def test_persistent_read_methods_map_mysql_rows_for_dashboard(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        manager.is_connected = True
        cursor = manager.db_conn.cursor.return_value
        cursor.fetchall.side_effect = [
            [("node-01", "127.0.0.1", 12, 34, 56, 7, "OFFLINE", "2025-01-01", "2024-12-01")],
            [
                (20, 30, 40, 2, "2025-01-02 12:00:00"),
                (10, 20, 30, 1, "2025-01-01 12:00:00"),
            ],
            [("node-01", "CPU", 90, 80, "2025-01-03 12:00:00")],
        ]

        clients = manager.get_clients()
        history = manager.get_history("node-01", limit=2)
        alerts = manager.get_alerts(limit=1)

        self.assertEqual(clients[0]["status"], "OFFLINE")
        self.assertEqual(clients[0]["cpu"], 12.0)
        self.assertEqual(
            [sample["timestamp"] for sample in history],
            ["2025-01-01 12:00:00", "2025-01-02 12:00:00"],
        )
        self.assertEqual(alerts[0]["metric"], "CPU")
        self.assertEqual(alerts[0]["value"], 90.0)
        self.assertEqual(cursor.execute.call_count, 6)
        self.assertEqual(cursor.close.call_count, 6)


class StartupDatabaseTests(unittest.TestCase):
    def test_server_connects_to_database_before_serving(self) -> None:
        with patch.object(server, "ensure_ports_available"), patch.object(
            server.db_manager, "connect", return_value=True
        ) as connect, patch.object(
            server.db_manager, "mark_all_clients_offline", return_value=True
        ) as initialize_status:
            with patch.object(server.threading, "Thread") as thread:
                with patch.object(server.app, "run") as run:
                    server.start_services()

        connect.assert_called_once_with()
        initialize_status.assert_called_once_with()
        self.assertEqual(thread.call_count, 2)
        run.assert_called_once()

    def test_server_refuses_to_start_without_mysql(self) -> None:
        with patch.object(server, "ensure_ports_available"), patch.object(
            server.db_manager, "connect", return_value=False
        ):
            with patch.object(server.threading, "Thread") as thread:
                with self.assertRaises(SystemExit):
                    server.start_services()

        thread.assert_not_called()

    def test_server_manager_passes_inherited_mysql_configuration(self) -> None:
        manager = server_gui.ServerManagerGUI.__new__(server_gui.ServerManagerGUI)
        manager.process = None
        manager.tcp_port_var = _Value("18888")
        manager.http_port_var = _Value("18081")
        manager.write_log = Mock()
        manager.update_controls = Mock()
        inherited = {
            "MYSQL_HOST": "mysql-host",
            "MYSQL_PORT": "3307",
            "MYSQL_USER": "app-user",
            "MYSQL_PASSWORD": "environment-secret",
            "MYSQL_DB": "existing_database",
            "MYSQL_CREATE_DATABASE": "false",
        }

        with patch.dict(os.environ, inherited, clear=False):
            with patch.object(server_gui, "is_port_available", return_value=True):
                with patch.object(server_gui.subprocess, "Popen") as popen:
                    with patch.object(server_gui.threading, "Thread"):
                        manager.start_server()

        self.assertEqual(
            popen.call_args.args[0],
            [server_gui.sys.executable, "-m", "server.server"],
        )
        self.assertEqual(popen.call_args.kwargs["cwd"], str(server_gui.PROJECT_ROOT))
        child_environment = popen.call_args.kwargs["env"]
        for name, value in inherited.items():
            self.assertEqual(child_environment[name], value)


class PersistentDashboardAPITests(unittest.TestCase):
    def test_dashboard_reads_clients_history_and_alerts_from_mysql(self) -> None:
        saved_clients = dict(server.clients)
        server.clients.clear()
        self.addCleanup(server.clients.update, saved_clients)
        stored_client = {
            "name": "node-01",
            "ip": "127.0.0.1",
            "cpu": 12.0,
            "ram": 34.0,
            "disk": 56.0,
            "network": 7.0,
            "status": "OFFLINE",
            "last_seen": "2025-01-01 12:00:00",
            "registered_at": "2024-12-01 12:00:00",
        }
        stored_sample = {
            "cpu": 12.0,
            "ram": 34.0,
            "disk": 56.0,
            "network": 7.0,
            "timestamp": "2025-01-01 12:00:00",
        }
        stored_alert = {
            "client": "node-01",
            "metric": "cpu",
            "value": 90.0,
            "limit": 80.0,
            "timestamp": "2025-01-01 12:00:00",
        }
        client = server.app.test_client()

        with patch.object(server.db_manager, "get_clients", return_value=[stored_client]) as get_clients:
            with patch.object(server.db_manager, "get_history", return_value=[stored_sample]) as get_history:
                with patch.object(server.db_manager, "get_alerts", return_value=[stored_alert]) as get_alerts:
                    clients_response = client.get("/api/clients")
                    history_response = client.get("/api/clients/node-01/history")
                    alerts_response = client.get("/api/alerts")

        self.assertEqual(clients_response.status_code, 200)
        self.assertEqual(clients_response.get_json()["clients"][0]["status"], "OFFLINE")
        self.assertEqual(clients_response.get_json()["clients"][0]["cpu"], 12.0)
        self.assertEqual(
            clients_response.get_json()["clients"][0]["last_seen"],
            "2025-01-01 12:00:00",
        )
        self.assertEqual(history_response.get_json()["samples"], [stored_sample])
        self.assertEqual(alerts_response.get_json()["alerts"], [stored_alert])
        get_clients.assert_called_once_with()
        get_history.assert_called_once_with("node-01", server.SAMPLE_LIMIT)
        get_alerts.assert_called_once_with(100)

    def test_dashboard_reports_mysql_read_failure(self) -> None:
        client = server.app.test_client()
        with patch.object(
            server.db_manager,
            "get_clients",
            side_effect=RuntimeError("MySQL unavailable"),
        ):
            response = client.get("/api/clients")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "error")


class ClientForcedDisconnectTests(unittest.TestCase):
    def test_cli_stops_after_server_disconnect_response(self) -> None:
        fake_client = Mock()
        fake_client.health.return_value = {"status": "ok"}
        fake_client.register.return_value = {"status": "ok", "message": "REGISTERED"}
        fake_client.collect_system_metrics.return_value = {
            "cpu": 1.0,
            "ram": 2.0,
            "disk": 3.0,
            "network": 4.0,
        }
        fake_client.send_metrics.return_value = {
            "status": "error",
            "message": "DISCONNECTED",
            "raw": "ERROR|DISCONNECTED",
        }

        with patch.object(monitoring_client, "NetworkMonitoringClient", return_value=fake_client):
            with redirect_stdout(io.StringIO()):
                monitoring_client.run_cli("node-01", interval=1)

        fake_client.send_heartbeat.assert_not_called()
        fake_client.disconnect.assert_not_called()

    def test_client_detects_disconnect_response(self) -> None:
        self.assertTrue(
            monitoring_client.was_disconnected_by_server(
                {"status": "error", "message": "DISCONNECTED", "raw": "ERROR|DISCONNECTED"}
            )
        )
        self.assertFalse(monitoring_client.was_disconnected_by_server({"status": "ok"}))


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


if __name__ == "__main__":
    unittest.main()
