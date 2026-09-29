from __future__ import annotations

import copy
import io
import json
import logging
import os
import socket
import threading
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from client import monitoring_client, process_monitor, tcp_client
from common import database
from common import logging_config
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


class LoggingConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_logger = logging.getLogger()
        self.previous_handlers = list(self.root_logger.handlers)
        self.previous_level = self.root_logger.level
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.log_directory = Path(self.temporary_directory.name)
        self.previous_clients = copy.deepcopy(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)

    def tearDown(self) -> None:
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)
        for handler in list(self.root_logger.handlers):
            if handler not in self.previous_handlers:
                self.root_logger.removeHandler(handler)
                handler.close()
        self.root_logger.setLevel(self.previous_level)

    def test_configures_console_rotating_file_level_and_is_idempotent(self) -> None:
        log_path = self.log_directory / "server.log"
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}, clear=False):
            logging_config.configure_logging("server", log_path)
            first_handlers = list(self.root_logger.handlers)
            logging_config.configure_logging("server", log_path)

        self.assertEqual(self.root_logger.level, logging.DEBUG)
        monitor_handlers = [
            handler
            for handler in self.root_logger.handlers
            if getattr(handler, "_monitor_console_handler", False)
            or getattr(handler, "_monitor_log_path", None) == str(log_path.resolve())
        ]
        self.assertEqual(len(monitor_handlers), 2)
        self.assertEqual(len(first_handlers), len(self.root_logger.handlers))
        file_handler = next(
            handler
            for handler in monitor_handlers
            if isinstance(handler, logging_config.RotatingFileHandler)
        )
        self.assertEqual(file_handler.maxBytes, logging_config.MAX_LOG_BYTES)
        self.assertEqual(file_handler.backupCount, logging_config.BACKUP_COUNT)
        self.assertTrue(log_path.is_file())

    def test_invalid_level_falls_back_to_info_and_logs_warning(self) -> None:
        log_path = self.log_directory / "invalid-level.log"
        with patch.dict(os.environ, {"LOG_LEVEL": "NOT_A_LEVEL"}, clear=False):
            logging_config.configure_logging("client", log_path)
        for handler in self.root_logger.handlers:
            handler.flush()

        self.assertEqual(self.root_logger.level, logging.INFO)
        self.assertIn("Invalid LOG_LEVEL", log_path.read_text(encoding="utf-8"))

    def test_logging_events_write_to_file_and_redact_secrets_and_tracebacks(self) -> None:
        log_path = self.log_directory / "safe.log"
        logging_config.configure_logging("server", log_path)
        logger = logging.getLogger("logging-test")
        logger.info(
            "Client registered; MYSQL_PASSWORD=%s MONITOR_ADMIN_TOKEN=%s",
            "db-secret-value",
            "admin-secret-value",
        )
        try:
            raise RuntimeError("Authorization: Bearer bearer-secret-value")
        except RuntimeError:
            logger.exception("Database operation failed")
        for handler in self.root_logger.handlers:
            handler.flush()
        content = log_path.read_text(encoding="utf-8")

        self.assertIn("Client registered", content)
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("db-secret-value", content)
        self.assertNotIn("admin-secret-value", content)
        self.assertNotIn("bearer-secret-value", content)

    def test_server_registration_heartbeat_logout_and_error_are_logged(self) -> None:
        log_path = self.log_directory / "server-events.log"
        server.clients.clear()
        server.disconnected_clients.clear()

        with patch.dict(
            os.environ,
            {"LOG_LEVEL": "DEBUG", "LOG_FILE": str(log_path)},
            clear=False,
        ):
            with patch.object(server, "ensure_ports_available"):
                with patch.object(server.db_manager, "connect", return_value=True):
                    with patch.object(
                        server.db_manager,
                        "mark_all_clients_offline",
                        return_value=True,
                    ):
                        with patch.object(server.threading, "Thread") as thread:
                            with patch.object(server.app, "run"):
                                with patch.object(server, "ADMIN_TOKEN", ""):
                                    server.start_services()
                            self.assertEqual(thread.call_count, 2)
            with patch.object(server.db_manager, "register_client", return_value=True):
                with patch.object(server.db_manager, "update_heartbeat", return_value=True):
                    with patch.object(server.db_manager, "update_status", return_value=True):
                        listener = socket.socket(
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                        )
                        listener.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_REUSEADDR,
                            1,
                        )
                        listener.bind(("127.0.0.1", 0))
                        listener.listen(5)
                        listener.settimeout(0.1)
                        stop_listener = threading.Event()
                        handlers: list[threading.Thread] = []

                        def accept_connections() -> None:
                            while not stop_listener.is_set():
                                try:
                                    connection, address = listener.accept()
                                except socket.timeout:
                                    continue
                                except OSError:
                                    return
                                handler = threading.Thread(
                                    target=server.tcp_client_session,
                                    args=(connection, address),
                                )
                                handlers.append(handler)
                                handler.start()

                        accept_thread = threading.Thread(
                            target=accept_connections
                        )
                        accept_thread.start()
                        tcp_peer = tcp_client.TCPClient(
                            host="127.0.0.1",
                            port=listener.getsockname()[1],
                            default_name="logging-node",
                        )
                        try:
                            self.assertEqual(tcp_peer.register()["status"], "ok")
                            self.assertEqual(
                                tcp_peer.heartbeat()["raw"],
                                "OK|HEARTBEAT",
                            )
                            self.assertEqual(
                                tcp_peer.disconnect()["raw"],
                                "OK|LOGOUT",
                            )
                            with socket.create_connection(
                                listener.getsockname(),
                                timeout=2,
                            ) as bad_client:
                                bad_client.sendall(
                                    b"SYSTEM|logging-node|CPU=invalid\n"
                                )
                                self.assertTrue(
                                    bad_client.recv(4096).startswith(b"ERROR|")
                                )
                        finally:
                            stop_listener.set()
                            listener.close()
                            accept_thread.join(timeout=2)
                            self.assertFalse(accept_thread.is_alive())
                            for handler in handlers:
                                handler.join(timeout=2)
                                self.assertFalse(handler.is_alive())
            with patch.object(server, "ADMIN_TOKEN", "admin-token-sentinel"):
                with server.app.test_client() as http_client:
                    denied = http_client.post(
                        "/api/clients/logging-node/commands",
                        headers={"X-Admin-Token": "invalid-token-sentinel"},
                        json={"command": "PING"},
                    )
                self.assertEqual(denied.status_code, 401)
        for handler in self.root_logger.handlers:
            handler.flush()
        content = log_path.read_text(encoding="utf-8")

        self.assertIn("Starting monitoring services", content)
        self.assertIn("HTTP dashboard listening", content)
        self.assertIn("registered from 127.0.0.1", content)
        self.assertIn("Heartbeat received from client logging-node", content)
        self.assertIn("Client logging-node logged out", content)
        self.assertIn("Rejected malformed SYSTEM message", content)
        self.assertIn("invalid credentials", content)
        self.assertNotIn("admin-token-sentinel", content)
        self.assertNotIn("invalid-token-sentinel", content)

    def test_client_lifecycle_events_use_client_log(self) -> None:
        log_path = self.log_directory / "client-events.log"
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}, clear=False):
            logging_config.configure_logging("client", log_path)
        client = monitoring_client.NetworkMonitoringClient(
            name="client-log-node",
            host="localhost",
        )
        client.tcp_client = Mock()
        client.tcp_client.register.return_value = {"status": "ok"}
        client.tcp_client.heartbeat.return_value = {"status": "ok"}
        client.tcp_client.disconnect.return_value = {"status": "ok"}

        self.assertEqual(client.register()["status"], "ok")
        self.assertEqual(client.send_heartbeat()["status"], "ok")
        self.assertEqual(client.disconnect()["status"], "ok")
        for handler in self.root_logger.handlers:
            handler.flush()
        content = log_path.read_text(encoding="utf-8")

        self.assertIn("registered with monitoring server", content)
        self.assertIn("heartbeat completed", content)
        self.assertIn("disconnected from monitoring server", content)


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

    def test_dashboard_explains_process_tracking_authentication_errors(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'id="process-error" class="process-message process-error" role="alert"',
            response.data,
        )
        self.assertIn(
            "MONITOR_ADMIN_TOKEN không hợp lệ. Hãy nhập lại đúng token "
            "đang cấu hình trên server.".encode("utf-8"),
            response.data,
        )
        self.assertIn(
            "kiểm tra MONITOR_ADMIN_TOKEN trên server và client giống nhau".encode(
                "utf-8"
            ),
            response.data,
        )
        self.assertIn(
            "Máy khách chưa phản hồi. Hãy kiểm tra máy khách còn trực tuyến, "
            "có gửi heartbeat và hỗ trợ theo dõi tiến trình.".encode("utf-8"),
            response.data,
        )
        self.assertIn(b"function formatProcessTrackingError(error)", response.data)
        self.assertIn(b"showProcessError(error)", response.data)

    def test_dashboard_renders_last_seen_column_and_missing_value_fallback(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<th>Last Seen</th>", response.data)
        self.assertIn(b"colspan=\"9\"", response.data)
        self.assertIn(b"renderLastSeen(c.last_seen)", response.data)
        self.assertIn(b"value == null || value === '' ? ", response.data)
        self.assertIn(b"escapeHtml(value)", response.data)
        self.assertIn(b"escapeHtml(c.status)", response.data)

    def test_dashboard_renders_network_rates_with_units(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Network (Up / Down)", response.data)
        self.assertIn(b"formatRate(c.upload_bytes_per_sec)", response.data)
        self.assertIn(b"formatRate(c.download_bytes_per_sec)", response.data)
        self.assertIn(b"upload_bytes_per_sec", response.data)
        self.assertIn(b"download_bytes_per_sec", response.data)
        self.assertIn(b"KiB/s", response.data)

    def test_system_accepts_network_traffic_fields_and_rejects_invalid_rates(self) -> None:
        metrics = {
            "cpu": 12.0,
            "ram": 34.0,
            "disk": 56.0,
            "network": 7.0,
            "upload_bytes_per_sec": 125.5,
            "download_bytes_per_sec": 256.25,
            "packets_sent": 42,
            "packets_recv": 84,
        }
        with patch.object(
            server.db_manager,
            "update_metrics",
            wraps=server.db_manager.update_metrics,
        ) as update_metrics:
            response = server.handle_message(
                "SYSTEM|node-01|CPU=12|RAM=34|DISK=56|NETWORK=7|"
                "UPLOAD_BPS=125.5|DOWNLOAD_BPS=256.25|"
                "PACKETS_SENT=42|PACKETS_RECV=84",
                ("127.0.0.1", 30000),
            )

        self.assertEqual(response, "OK|SYSTEM")
        update_metrics.assert_called_once_with("node-01", metrics)

        invalid_response = server.handle_message(
            "SYSTEM|node-01|UPLOAD_BPS=nan",
            ("127.0.0.1", 30000),
        )
        self.assertTrue(invalid_response.startswith("ERROR|"))

    def test_system_accepts_null_network_counters_and_legacy_messages(self) -> None:
        with patch.object(
            server.db_manager,
            "update_metrics",
            wraps=server.db_manager.update_metrics,
        ) as update_metrics:
            legacy_response = server.handle_message(
                "SYSTEM|node-01|CPU=12|NETWORK=7",
                ("127.0.0.1", 30000),
            )
            new_response = server.handle_message(
                "SYSTEM|node-01|UPLOAD_BPS=null|DOWNLOAD_BPS=null|"
                "PACKETS_SENT=null|PACKETS_RECV=null",
                ("127.0.0.1", 30000),
            )

        self.assertEqual(legacy_response, "OK|SYSTEM")
        self.assertEqual(new_response, "OK|SYSTEM")
        self.assertEqual(
            update_metrics.call_args_list[0].args[1],
            {"cpu": 12.0, "network": 7.0},
        )
        self.assertEqual(
            update_metrics.call_args_list[1].args[1],
            {
                "upload_bytes_per_sec": None,
                "download_bytes_per_sec": None,
                "packets_sent": None,
                "packets_recv": None,
            },
        )


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
                    "upload_bytes_per_sec": float(100 + index),
                    "download_bytes_per_sec": float(200 + index),
                    "packets_sent": 300 + index,
                    "packets_recv": 400 + index,
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
                    lambda index: "SYSTEM|{}|CPU={}|RAM={}|DISK={}|NETWORK={}"
                    "|UPLOAD_BPS={}|DOWNLOAD_BPS={}|PACKETS_SENT={}|PACKETS_RECV={}".format(
                        names[index],
                        metrics[index]["cpu"],
                        metrics[index]["ram"],
                        metrics[index]["disk"],
                        metrics[index]["network"],
                        metrics[index]["upload_bytes_per_sec"],
                        metrics[index]["download_bytes_per_sec"],
                        metrics[index]["packets_sent"],
                        metrics[index]["packets_recv"],
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
        self.previous_controlled_requests = copy.deepcopy(
            server.controlled_command_requests
        )
        self.previous_process_snapshots = copy.deepcopy(server.process_snapshots)
        server.ADMIN_TOKEN = "test-process-admin-token"
        server.clients.clear()
        server.disconnected_clients.clear()
        server.process_requests.clear()
        server.controlled_command_requests.clear()
        server.process_snapshots.clear()

        self.database_patches = (
            patch.object(server.db_manager, "register_client", return_value=True),
            patch.object(server.db_manager, "update_heartbeat", return_value=True),
            patch.object(server.db_manager, "update_status", return_value=True),
            patch.object(server.db_manager, "record_process_activity", return_value=True),
            patch.object(server.db_manager, "get_process_activity", return_value=[]),
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
        server.controlled_command_requests.clear()
        server.controlled_command_requests.update(self.previous_controlled_requests)
        server.process_snapshots.clear()
        server.process_snapshots.update(self.previous_process_snapshots)

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
        self.assertEqual(result["raw"], "OK|PROCESS_LIST")
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
            "REGISTER|process-node|127.0.0.1|8888|PROCESS_LIST_V1|CONTROLLED_COMMANDS_V1|PROCESS_MANAGEMENT_V1",
        )
        self.assertEqual(
            server.handle_message(message, ("127.0.0.1", 30000)),
            "OK|REGISTERED",
        )
        self.assertTrue(server.clients["process-node"]["process_list_capable"])
        self.assertTrue(server.clients["process-node"]["controlled_commands_capable"])

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


class ControlledCommandTests(unittest.TestCase):
    SAMPLE_PROCESS = {
        "pid": 123,
        "name": "worker",
        "username": None,
        "cpu_percent": 12.5,
        "memory_percent": 3.25,
        "status": "running",
    }
    NETWORK_INFO = {
        "bytes_sent": 1024,
        "bytes_recv": 2048,
        "packets_sent": 10,
        "packets_recv": 20,
        "upload_bytes_per_sec": 15.5,
        "download_bytes_per_sec": 26.5,
    }

    def setUp(self) -> None:
        self.previous_admin_token = server.ADMIN_TOKEN
        self.previous_clients = copy.deepcopy(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)
        self.previous_process_requests = copy.deepcopy(server.process_requests)
        self.previous_controlled_requests = copy.deepcopy(
            server.controlled_command_requests
        )
        server.ADMIN_TOKEN = "test-command-admin-token"
        server.clients.clear()
        server.disconnected_clients.clear()
        server.process_requests.clear()
        server.controlled_command_requests.clear()
        for method in (
            "register_client",
            "update_heartbeat",
            "update_status",
            "update_metrics",
            "add_alert",
        ):
            active_patch = patch.object(
                server.db_manager,
                method,
                return_value=True,
            )
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
        server.controlled_command_requests.clear()
        server.controlled_command_requests.update(self.previous_controlled_requests)

    def admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": server.ADMIN_TOKEN}

    def register(self, name: str) -> None:
        response = server.handle_message(
            f"REGISTER|{name}|127.0.0.1|8888|PROCESS_LIST_V1|"
            f"{server.CONTROLLED_COMMANDS_CAPABILITY}",
            ("127.0.0.1", 30000),
        )
        self.assertEqual(response, "OK|REGISTERED")

    def queue(self, name: str, command: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/clients/{name}/commands",
            headers=self.admin_headers(),
            json={"command": command},
        )
        self.assertEqual(response.status_code, 202)
        return response.get_json()["request"]

    def deliver_and_reply(
        self,
        name: str,
        command_name: str,
        *,
        patches: tuple[Any, ...] = (),
    ) -> dict[str, Any]:
        request = self.queue(name, command_name)
        address = ("127.0.0.1", 30000)
        command = server.handle_message(f"HEARTBEAT|{name}", address)
        request_id = request["request_id"]
        fake_socket = Mock()
        fake_socket.recv.return_value = b"OK|COMMAND\n"
        with patches[0] if patches else nullcontext():
            client_result = tcp_client.TCPClient(
                default_name=name
            )._answer_server_command(fake_socket, command, name)
        self.assertEqual(client_result["status"], "ok")
        sent_response = fake_socket.sendall.call_args.args[0].decode().strip()
        self.assertTrue(sent_response.startswith(f"RESPONSE|{request_id}|"))
        self.assertEqual(server.handle_message(sent_response, address), "OK|COMMAND")
        result = self.client.get(
            f"/api/clients/{name}/commands",
            headers=self.admin_headers(),
        )
        self.assertEqual(result.status_code, 200)
        return result.get_json()["request"]

    def test_admin_api_accepts_only_allowlisted_commands(self) -> None:
        self.register("allowlist-node")
        for body, expected in (
            ({"command": "RUN_SHELL"}, 400),
            ({"command": "PING", "extra": "value"}, 400),
            (["PING"], 400),
        ):
            response = self.client.post(
                "/api/clients/allowlist-node/commands",
                headers=self.admin_headers(),
                json=body,
            )
            self.assertEqual(response.status_code, expected)
        unauthorized = self.client.post(
            "/api/clients/allowlist-node/commands",
            json={"command": "PING"},
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(server.controlled_command_requests, {})

    def test_all_four_commands_return_correlated_validated_results(self) -> None:
        self.register("command-node")
        commands = ("PING", "GET_INFO", "GET_PROCESS_LIST", "GET_NETWORK_INFO")
        for command in commands:
            patches = []
            if command == "GET_PROCESS_LIST":
                patches.append(
                    patch.object(
                        tcp_client,
                        "collect_process_list",
                        return_value=[self.SAMPLE_PROCESS],
                    )
                )
            elif command == "GET_NETWORK_INFO":
                patches.append(
                    patch.object(
                        tcp_client.TCPClient,
                        "_collect_network_info",
                        return_value=self.NETWORK_INFO,
                    )
                )
            with self.subTest(command=command):
                result = self.deliver_and_reply(
                    "command-node",
                    command,
                    patches=tuple(patches),
                )
                self.assertEqual(result["command"], command)
                self.assertEqual(result["status"], "complete")
                if command == "PING":
                    self.assertEqual(result["result"], "PONG")
                elif command == "GET_INFO":
                    self.assertEqual(
                        set(result["result"]),
                        {
                            "client_name",
                            "hostname",
                            "os",
                            "os_release",
                            "machine",
                            "python_version",
                        },
                    )
                elif command == "GET_PROCESS_LIST":
                    self.assertEqual(result["result"], [self.SAMPLE_PROCESS])
                else:
                    self.assertEqual(result["result"], self.NETWORK_INFO)

    def test_malformed_client_command_and_response_are_rejected(self) -> None:
        fake_socket = Mock()
        client = tcp_client.TCPClient(default_name="command-node")
        rejected = client._answer_server_command(
            fake_socket,
            "COMMAND|id-1|RUN_SHELL",
            "command-node",
        )
        self.assertEqual(rejected["status"], "error")
        fake_socket.sendall.assert_not_called()

        self.register("malformed-node")
        request = self.queue("malformed-node", "PING")
        address = ("127.0.0.1", 30000)
        server.handle_message("HEARTBEAT|malformed-node", address)
        response = server.handle_message(
            f"RESPONSE|{request['request_id']}|NOT_PONG",
            address,
        )
        self.assertEqual(response, "ERROR|MALFORMED_COMMAND_RESPONSE")
        self.assertEqual(
            server.controlled_command_requests["malformed-node"]["status"],
            "delivered",
        )

    def test_command_result_is_bound_to_delivery_peer(self) -> None:
        self.register("peer-bound-node")
        request = self.queue("peer-bound-node", "PING")
        address = ("127.0.0.1", 30000)
        command = server.handle_message("HEARTBEAT|peer-bound-node", address)
        response = server.handle_message(
            f"RESPONSE|{request['request_id']}|PONG",
            ("127.0.0.1", 30001),
        )
        self.assertEqual(response, "ERROR|INVALID_COMMAND_REQUEST")
        self.assertEqual(
            server.controlled_command_requests["peer-bound-node"]["status"],
            "delivered",
        )
        self.assertTrue(command.startswith(f"COMMAND|{request['request_id']}|PING"))

    def test_command_timeout_and_disconnect_are_reported(self) -> None:
        self.register("timeout-node")
        request = self.queue("timeout-node", "PING")
        server.controlled_command_requests["timeout-node"]["expires_at"] = (
            time.monotonic() - 1
        )
        self.assertEqual(
            server.handle_message(
                "HEARTBEAT|timeout-node",
                ("127.0.0.1", 30000),
            ),
            "OK|HEARTBEAT",
        )
        self.assertEqual(
            server.controlled_command_requests["timeout-node"]["status"],
            "timeout",
        )
        self.assertEqual(
            self.client.get(
                "/api/clients/timeout-node/commands",
                headers=self.admin_headers(),
            ).get_json()["request"]["status"],
            "timeout",
        )

        self.register("disconnect-node")
        request = self.queue("disconnect-node", "PING")
        address = ("127.0.0.1", 30000)
        server.handle_message("HEARTBEAT|disconnect-node", address)
        server.handle_message("LOGOUT|disconnect-node", address)
        response = server.handle_message(
            f"RESPONSE|{request['request_id']}|PONG",
            address,
        )
        self.assertEqual(response, "ERROR|INVALID_COMMAND_REQUEST")

    def test_process_collection_failure_is_reported_without_error_details(self) -> None:
        self.register("failed-command-node")
        request = self.queue("failed-command-node", "GET_PROCESS_LIST")
        address = ("127.0.0.1", 30000)
        command = server.handle_message(
            "HEARTBEAT|failed-command-node",
            address,
        )
        fake_socket = Mock()
        fake_socket.recv.return_value = b"OK|COMMAND\n"
        with patch.object(
            tcp_client,
            "collect_process_list",
            side_effect=RuntimeError("password must not appear"),
        ):
            result = tcp_client.TCPClient(
                default_name="failed-command-node"
            )._answer_server_command(fake_socket, command, "failed-command-node")

        sent_response = fake_socket.sendall.call_args.args[0].decode().strip()
        self.assertEqual(
            sent_response,
            f"COMMAND_ERROR|{request['request_id']}|UNAVAILABLE",
        )
        self.assertNotIn("password", sent_response)
        self.assertEqual(server.handle_message(sent_response, address), "OK|COMMAND")
        self.assertEqual(result["status"], "error")
        request_status = self.client.get(
            "/api/clients/failed-command-node/commands",
            headers=self.admin_headers(),
        ).get_json()["request"]
        self.assertEqual(request_status["status"], "error")
        self.assertIsNone(request_status["result"])

    def test_network_command_reports_first_sample_rates_and_counter_reset(self) -> None:
        samples = [
            Mock(bytes_sent=100, bytes_recv=200, packets_sent=3, packets_recv=4),
            Mock(bytes_sent=160, bytes_recv=240, packets_sent=5, packets_recv=7),
            Mock(bytes_sent=10, bytes_recv=20, packets_sent=1, packets_recv=2),
        ]
        psutil_stub = Mock(net_io_counters=Mock(side_effect=samples))
        peer = tcp_client.TCPClient(default_name="network-node")
        with patch.object(tcp_client, "psutil", psutil_stub):
            with patch.object(
                tcp_client.time,
                "monotonic",
                side_effect=[10.0, 12.0, 14.0],
            ):
                first = peer._collect_network_info()
                second = peer._collect_network_info()
                reset = peer._collect_network_info()

        self.assertEqual(first["upload_bytes_per_sec"], 0.0)
        self.assertEqual(first["download_bytes_per_sec"], 0.0)
        self.assertEqual(second["upload_bytes_per_sec"], 30.0)
        self.assertEqual(second["download_bytes_per_sec"], 20.0)
        self.assertEqual(reset["upload_bytes_per_sec"], 0.0)
        self.assertEqual(reset["download_bytes_per_sec"], 0.0)

    def test_network_command_returns_null_when_counters_are_unavailable(self) -> None:
        with patch.object(tcp_client, "psutil", None):
            result = tcp_client.TCPClient()._collect_network_info()

        self.assertEqual(
            result,
            {
                "bytes_sent": None,
                "bytes_recv": None,
                "packets_sent": None,
                "packets_recv": None,
                "upload_bytes_per_sec": None,
                "download_bytes_per_sec": None,
            },
        )

    def test_ping_command_completes_over_loopback_tcp(self) -> None:
        stop_listener = threading.Event()
        handler_threads: list[threading.Thread] = []
        listener_errors: list[OSError] = []
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.1)

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
        peer = tcp_client.TCPClient(
            host="127.0.0.1",
            port=listener.getsockname()[1],
            default_name="loopback-command-node",
        )
        try:
            self.assertEqual(peer.register()["status"], "ok")
            queued = self.queue("loopback-command-node", "PING")
            heartbeat = peer.heartbeat()
            self.assertEqual(heartbeat["status"], "ok")
            self.assertEqual(heartbeat["command_status"], "complete")
            self.assertEqual(heartbeat["raw"], "OK|COMMAND")
            result = self.client.get(
                "/api/clients/loopback-command-node/commands",
                headers=self.admin_headers(),
            ).get_json()["request"]
            self.assertEqual(result["request_id"], queued["request_id"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["result"], "PONG")
        finally:
            stop_listener.set()
            listener.close()
            accept_thread.join(timeout=2)
            self.assertFalse(accept_thread.is_alive())
            for handler in handler_threads:
                handler.join(timeout=2)
                self.assertFalse(handler.is_alive())
            self.assertEqual(listener_errors, [])


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
                    for index in range(process_monitor.TOP_N + 15)
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
            with self.assertRaises(RuntimeError):
                process_monitor.collect_process_snapshot()

    def test_process_snapshot_includes_processes_beyond_legacy_top_n(self) -> None:
        class FakeProcess:
            def __init__(self, pid: int):
                self.info = {
                    "pid": pid,
                    "name": f"process-{pid}.exe",
                    "username": "monitor",
                    "cpu_percent": 0.1,
                    "memory_percent": 0.1,
                    "status": "running",
                }

        class FakePsutil:
            Error = RuntimeError
            NoSuchProcess = type("NoSuchProcess", (Exception,), {})
            ZombieProcess = type("ZombieProcess", (NoSuchProcess,), {})
            AccessDenied = type("AccessDenied", (Exception,), {})

            @staticmethod
            def process_iter(*, attrs, ad_value):
                assert "pid" in attrs and "name" in attrs and ad_value is None
                return [FakeProcess(0), *(FakeProcess(pid) for pid in range(1, 76))]

        with patch.object(process_monitor, "psutil", FakePsutil):
            snapshot = process_monitor.collect_process_snapshot()
            legacy_list = process_monitor.collect_process_list()

        self.assertEqual(len(snapshot), 75)
        self.assertEqual(len(legacy_list), process_monitor.TOP_N)
        self.assertIn(75, {process["pid"] for process in snapshot})
        self.assertNotIn(0, {process["pid"] for process in snapshot})

    def test_process_snapshot_skips_stale_stopped_entries_only(self) -> None:
        records = []

        class FakeProcess:
            def __init__(self, info):
                self.info = info

        class FakePsutil:
            Error = RuntimeError
            NoSuchProcess = type("NoSuchProcess", (Exception,), {})
            ZombieProcess = type("ZombieProcess", (NoSuchProcess,), {})
            AccessDenied = type("AccessDenied", (Exception,), {})
            STATUS_STOPPED = "stopped"

            @staticmethod
            def process_iter(*, attrs, ad_value):
                assert "pid" in attrs and "name" in attrs and ad_value is None
                return [FakeProcess(record) for record in records]

        with patch.object(process_monitor, "psutil", FakePsutil):
            records.append(
                {
                    "pid": 100,
                    "name": "",
                    "username": None,
                    "cpu_percent": None,
                    "memory_percent": None,
                    "status": "stopped",
                }
            )
            self.assertEqual(process_monitor.collect_process_snapshot(), [])

            records[:] = [
                {
                    "pid": 101,
                    "name": "",
                    "username": None,
                    "cpu_percent": None,
                    "memory_percent": None,
                    "status": "running",
                }
            ]
            with self.assertRaisesRegex(RuntimeError, "incomplete snapshot"):
                process_monitor.collect_process_snapshot()


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
                                "SYSTEM|{}|CPU={}|RAM={}|DISK={}|NETWORK={}"
                                "|UPLOAD_BPS={}|DOWNLOAD_BPS={}"
                                "|PACKETS_SENT={}|PACKETS_RECV={}".format(
                                    names[index],
                                    metric["cpu"],
                                    metric["ram"],
                                    metric["disk"],
                                    metric["network"],
                                    metric["upload_bytes_per_sec"],
                                    metric["download_bytes_per_sec"],
                                    metric["packets_sent"],
                                    metric["packets_recv"],
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

    def test_process_activity_writes_events_and_caps_history_per_client(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        manager.is_connected = True
        events = [
            {"pid": 1234, "name": "notepad.exe", "event_type": "STARTED"},
            {"pid": 8912, "name": "spotify.exe", "event_type": "STOPPED"},
        ]

        with patch.object(manager, "_check_connection", return_value=True):
            self.assertTrue(manager.record_process_activity("PC-01", events))

        statements = [
            call.args[0]
            for call in manager.db_conn.cursor.return_value.execute.call_args_list
        ]
        self.assertEqual(sum("INSERT INTO process_activity" in sql for sql in statements), 2)
        retention_query = next(sql for sql in statements if "DELETE FROM process_activity" in sql)
        self.assertIn("WHERE client_key = %s", retention_query)
        self.assertIn(str(database.PROCESS_ACTIVITY_HISTORY_LIMIT), str(
            manager.db_conn.cursor.return_value.execute.call_args_list[-1].args[1]
        ))
        manager.db_conn.commit.assert_called_once_with()

    def test_process_activity_reader_normalizes_recent_event_rows(self) -> None:
        manager = database.DatabaseManager()
        event_time = database.datetime(2026, 9, 29, 8, 52, 31)
        with patch.object(
            manager,
            "_read_rows",
            return_value=[(1234, "notepad.exe", "STARTED", event_time)],
        ) as read_rows:
            events = manager.get_process_activity("PC-01", 50)

        self.assertEqual(
            events,
            [{
                "pid": 1234,
                "process_name": "notepad.exe",
                "event_type": "STARTED",
                "timestamp": "2026-09-29T08:52:31",
            }],
        )
        self.assertEqual(read_rows.call_args.args[2], ("pc-01", 50))

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
                {
                    "cpu": 12.0,
                    "ram": 34.0,
                    "disk": 56.0,
                    "network": 7.0,
                    "upload_bytes_per_sec": 128.5,
                    "download_bytes_per_sec": 256.25,
                    "packets_sent": 42,
                    "packets_recv": 84,
                },
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
        metrics_update = next(
            call for call in manager.db_conn.cursor.return_value.execute.call_args_list
            if "UPDATE clients" in call.args[0]
        )
        self.assertIn("upload_bytes_per_sec = %s", metrics_update.args[0])
        self.assertIn("download_bytes_per_sec = %s", metrics_update.args[0])
        self.assertEqual(metrics_update.args[1][4:8], (128.5, 256.25, 42, 84))
        self.assertEqual(manager.db_conn.commit.call_count, 3)
        self.assertEqual(manager.db_conn.rollback.call_count, 3)

    def test_schema_adds_nullable_network_traffic_columns(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        cursor = manager.db_conn.cursor.return_value
        cursor.fetchone.return_value = None
        manager.is_connected = True

        manager._create_tables()

        alters = [
            call.args[0]
            for call in cursor.execute.call_args_list
            if call.args[0].startswith("ALTER TABLE")
        ]
        self.assertEqual(len(alters), 8)
        self.assertTrue(
            any(
                "CREATE TABLE IF NOT EXISTS process_activity" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )
        self.assertTrue(any("clients` ADD COLUMN `upload_bytes_per_sec`" in query for query in alters))
        self.assertTrue(any("history` ADD COLUMN `download_bytes_per_sec`" in query for query in alters))
        self.assertTrue(any("packets_sent` BIGINT UNSIGNED NULL" in query for query in alters))
        manager.db_conn.commit.assert_called_once_with()

    def test_schema_migration_does_not_alter_existing_network_columns(self) -> None:
        manager = database.DatabaseManager()
        manager.db_conn = Mock()
        cursor = manager.db_conn.cursor.return_value
        cursor.fetchone.return_value = (1,)
        manager.is_connected = True

        manager._create_tables()

        self.assertFalse(
            any(
                call.args[0].startswith("ALTER TABLE")
                for call in cursor.execute.call_args_list
            )
        )

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
            [(
                "node-01", "127.0.0.1", 12, 34, 56, 7,
                128.5, 256.25, 42, 84,
                "OFFLINE", "2025-01-01", "2024-12-01",
            )],
            [
                (20, 30, 40, 2, 128.5, 256.25, 42, 84, "2025-01-02 12:00:00"),
                (10, 20, 30, 1, 64.0, 128.0, 20, 40, "2025-01-01 12:00:00"),
            ],
            [("node-01", "CPU", 90, 80, "2025-01-03 12:00:00")],
        ]

        clients = manager.get_clients()
        history = manager.get_history("node-01", limit=2)
        alerts = manager.get_alerts(limit=1)

        self.assertEqual(clients[0]["status"], "OFFLINE")
        self.assertEqual(clients[0]["cpu"], 12.0)
        self.assertEqual(clients[0]["upload_bytes_per_sec"], 128.5)
        self.assertEqual(clients[0]["packets_sent"], 42)
        self.assertEqual(history[0]["download_bytes_per_sec"], 128.0)
        self.assertEqual(history[1]["packets_recv"], 84)
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
        with patch.object(server, "configure_logging"), patch.object(
            server, "ensure_ports_available"
        ), patch.object(
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

    def test_server_starts_without_admin_token_and_disables_admin_operations(self) -> None:
        with patch.object(server, "ADMIN_TOKEN", ""), patch.object(
            server, "configure_logging"
        ), patch.object(
            server, "ensure_ports_available"
        ), patch.object(
            server.db_manager, "connect", return_value=True
        ), patch.object(
            server.db_manager, "mark_all_clients_offline", return_value=True
        ), patch.object(server.logger, "warning") as warning:
            with patch.object(server.threading, "Thread") as thread:
                with patch.object(server.app, "run") as run:
                    server.start_services()

        warning.assert_called_once_with(
            "MONITOR_ADMIN_TOKEN is not configured. "
            "Administrative operations are disabled."
        )
        self.assertEqual(thread.call_count, 2)
        run.assert_called_once()

    def test_server_refuses_to_start_without_mysql(self) -> None:
        with patch.object(server, "configure_logging"), patch.object(
            server, "ensure_ports_available"
        ), patch.object(server.db_manager, "connect", return_value=False):
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

    def test_server_manager_shows_admin_configuration_without_exposing_token(
        self,
    ) -> None:
        manager = server_gui.ServerManagerGUI.__new__(server_gui.ServerManagerGUI)
        manager.admin_status_var = Mock()
        manager.admin_status_label = Mock()
        manager.process_control_var = Mock()
        manager.process_control_label = Mock()

        manager._update_admin_status(False)
        manager.admin_status_var.set.assert_called_with("NOT CONFIGURED")
        manager.process_control_var.set.assert_called_with("DISABLED")

        manager._update_admin_status(True)
        manager.admin_status_var.set.assert_called_with("CONFIGURED")
        manager.process_control_var.set.assert_called_with("ENABLED")


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
            "upload_bytes_per_sec": 128.5,
            "download_bytes_per_sec": 256.25,
            "packets_sent": 42,
            "packets_recv": 84,
            "status": "OFFLINE",
            "last_seen": "2025-01-01 12:00:00",
            "registered_at": "2024-12-01 12:00:00",
        }
        stored_sample = {
            "cpu": 12.0,
            "ram": 34.0,
            "disk": 56.0,
            "network": 7.0,
            "upload_bytes_per_sec": 128.5,
            "download_bytes_per_sec": 256.25,
            "packets_sent": 42,
            "packets_recv": 84,
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
            clients_response.get_json()["clients"][0]["upload_bytes_per_sec"],
            128.5,
        )
        self.assertEqual(
            clients_response.get_json()["clients"][0]["packets_recv"],
            84,
        )
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
            with patch.object(monitoring_client, "configure_logging"):
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


class NetworkTrafficCollectorTests(unittest.TestCase):
    @staticmethod
    def _fake_psutil(counters):
        return Mock(
            cpu_percent=Mock(return_value=10.0),
            virtual_memory=Mock(return_value=Mock(percent=20.0)),
            disk_usage=Mock(return_value=Mock(percent=30.0)),
            net_io_counters=Mock(side_effect=counters),
        )

    def test_collector_uses_elapsed_time_for_upload_and_download_rates(self) -> None:
        client = monitoring_client.NetworkMonitoringClient("node-01")
        fake_psutil = self._fake_psutil(
            [
                Mock(bytes_sent=100, bytes_recv=200, packets_sent=3, packets_recv=4),
                Mock(bytes_sent=300, bytes_recv=600, packets_sent=7, packets_recv=9),
            ]
        )
        with patch.object(monitoring_client, "psutil", fake_psutil):
            with patch.object(monitoring_client.time, "monotonic", side_effect=[10.0, 12.0]):
                first = client.collect_system_metrics()
                second = client.collect_system_metrics()

        self.assertEqual(first["upload_bytes_per_sec"], 0.0)
        self.assertEqual(first["download_bytes_per_sec"], 0.0)
        self.assertEqual(second["upload_bytes_per_sec"], 100.0)
        self.assertEqual(second["download_bytes_per_sec"], 200.0)
        self.assertEqual(second["packets_sent"], 7)
        self.assertEqual(second["packets_recv"], 9)

    def test_counter_reset_rebaselines_and_reports_zero_directional_rate(self) -> None:
        client = monitoring_client.NetworkMonitoringClient("node-01")
        fake_psutil = self._fake_psutil(
            [
                Mock(bytes_sent=1000, bytes_recv=2000, packets_sent=30, packets_recv=40),
                Mock(bytes_sent=10, bytes_recv=20, packets_sent=1, packets_recv=2),
                Mock(bytes_sent=110, bytes_recv=220, packets_sent=5, packets_recv=8),
            ]
        )
        with patch.object(monitoring_client, "psutil", fake_psutil):
            with patch.object(
                monitoring_client.time,
                "monotonic",
                side_effect=[1.0, 2.0, 3.0],
            ):
                client.collect_system_metrics()
                reset_sample = client.collect_system_metrics()
                next_sample = client.collect_system_metrics()

        self.assertEqual(reset_sample["upload_bytes_per_sec"], 0.0)
        self.assertEqual(reset_sample["download_bytes_per_sec"], 0.0)
        self.assertEqual(next_sample["upload_bytes_per_sec"], 100.0)
        self.assertEqual(next_sample["download_bytes_per_sec"], 200.0)

    def test_unavailable_network_counters_are_returned_as_null(self) -> None:
        client = monitoring_client.NetworkMonitoringClient("node-01")
        fake_psutil = self._fake_psutil([None])
        with patch.object(monitoring_client, "psutil", fake_psutil):
            with patch.object(monitoring_client.time, "monotonic", return_value=1.0):
                with self.assertLogs(monitoring_client.logger, level="WARNING"):
                    metrics = client.collect_system_metrics()

        self.assertEqual(metrics["network"], 0.0)
        self.assertIsNone(metrics["upload_bytes_per_sec"])
        self.assertIsNone(metrics["download_bytes_per_sec"])
        self.assertIsNone(metrics["packets_sent"])
        self.assertIsNone(metrics["packets_recv"])

    def test_protocol_keeps_legacy_field_and_adds_named_rates(self) -> None:
        client = tcp_client.TCPClient(default_name="node-01")
        legacy_message = client._build_message(
            "SYSTEM",
            {"name": "node-01", "cpu": 1, "ram": 2, "disk": 3, "network": 4},
        )
        extended_message = client._build_message(
            "SYSTEM",
            {
                "name": "node-01",
                "cpu": 1,
                "ram": 2,
                "disk": 3,
                "network": 4,
                "upload_bytes_per_sec": 128.5,
                "download_bytes_per_sec": 256.0,
                "packets_sent": 10,
                "packets_recv": None,
            },
        )

        self.assertEqual(
            legacy_message,
            "SYSTEM|node-01|CPU=1|RAM=2|DISK=3|NETWORK=4",
        )
        self.assertIn("NETWORK=4", extended_message)
        self.assertIn("UPLOAD_BPS=128.5", extended_message)
        self.assertIn("DOWNLOAD_BPS=256.0", extended_message)
        self.assertIn("PACKETS_SENT=10", extended_message)
        self.assertIn("PACKETS_RECV=null", extended_message)


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


if __name__ == "__main__":
    unittest.main()
