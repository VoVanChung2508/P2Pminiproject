from __future__ import annotations

import io
import os
import socket
import threading
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from client import monitoring_client
from common import database
from server import server
from server import server_gui


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
        self, client_port: int = 12345
    ) -> tuple[socket.socket, socket.socket, threading.Thread]:
        accepted_socket, client_socket = socket.socketpair()
        client_socket.settimeout(2)
        handler = threading.Thread(
            target=server.tcp_client_session,
            args=(accepted_socket, ("127.0.0.1", client_port)),
        )
        handler.start()
        return accepted_socket, client_socket, handler

    def exchange(self, client_socket: socket.socket, message: str) -> str:
        client_socket.sendall((message + "\n").encode("utf-8"))
        return client_socket.recv(4096).decode("utf-8").strip()

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

    def test_unexpected_send_error_is_logged_and_connection_is_closed(self) -> None:
        connection = Mock()
        connection.recv.return_value = b"REGISTER|node-04\n"
        connection.sendall.side_effect = OSError("send failure")

        with self.assertLogs(server.logger, level="ERROR") as captured:
            server.tcp_client_session(connection, ("127.0.0.1", 12350))

        connection.close.assert_called_once_with()
        self.assertTrue(any("TCP send failed" in entry for entry in captured.output))

    def test_multiple_clients_connect_concurrently(self) -> None:
        sessions = [self.open_session(12400 + index) for index in range(4)]
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
