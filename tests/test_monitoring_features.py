from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.assertEqual(cursor.execute.call_count, 3)
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
        self.assertEqual(cursor.execute.call_count, 3)
        self.assertEqual(cursor.close.call_count, 3)


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
