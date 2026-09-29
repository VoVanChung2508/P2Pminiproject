from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from client import process_monitor, tcp_client
from common.command_auth import sign_process_command, verify_process_command
from server import server


class ProcessTerminationTests(TestCase):
    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(NoSuchProcess):
        pass

    class AccessDenied(Exception):
        pass

    class TimeoutExpired(Exception):
        pass

    def setUp(self) -> None:
        self.process = Mock()
        self.process.name.return_value = "worker.exe"
        self.process.status.return_value = "running"
        self.process.is_running.return_value = True
        self.process.wait.return_value = 0
        self.psutil_stub = SimpleNamespace(
            Process=Mock(return_value=self.process),
            NoSuchProcess=self.NoSuchProcess,
            ZombieProcess=self.ZombieProcess,
            AccessDenied=self.AccessDenied,
            TimeoutExpired=self.TimeoutExpired,
            Error=Exception,
        )
        self.psutil_patch = patch.object(
            process_monitor,
            "psutil",
            self.psutil_stub,
        )
        self.psutil_patch.start()
        self.addCleanup(self.psutil_patch.stop)

    def test_terminates_a_valid_running_process_and_returns_its_name(self) -> None:
        result = process_monitor.terminate_process(1234)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "PROCESS_TERMINATED")
        self.assertEqual(result["pid"], 1234)
        self.assertEqual(result["name"], "worker.exe")
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_once()

    def test_rejects_non_integer_and_non_positive_pids(self) -> None:
        for pid in (True, 0, -1, 1.5, "1234"):
            with self.subTest(pid=pid):
                result = process_monitor.terminate_process(pid)
                self.assertEqual(result["code"], "INVALID_PID")
        self.psutil_stub.Process.assert_not_called()

    def test_reports_a_process_that_disappeared_before_termination(self) -> None:
        self.psutil_stub.Process.side_effect = self.NoSuchProcess()

        result = process_monitor.terminate_process(1234)

        self.assertEqual(result["code"], "PROCESS_NOT_FOUND")
        self.process.terminate.assert_not_called()

    def test_does_not_terminate_a_process_that_is_no_longer_running(self) -> None:
        self.process.is_running.return_value = False

        result = process_monitor.terminate_process(1234)

        self.assertEqual(result["code"], "PROCESS_NOT_FOUND")
        self.process.terminate.assert_not_called()

    def test_reports_access_denied_from_the_operating_system(self) -> None:
        self.process.terminate.side_effect = self.AccessDenied()

        result = process_monitor.terminate_process(1234)

        self.assertEqual(result["code"], "ACCESS_DENIED")

    def test_protects_the_agent_and_platform_critical_processes(self) -> None:
        with patch.object(process_monitor.os, "getpid", return_value=1234):
            own_process = process_monitor.terminate_process(1234)
        self.assertEqual(own_process["code"], "PROTECTED_PROCESS")
        self.process.terminate.assert_not_called()

        self.psutil_stub.Process.reset_mock()
        self.process.name.return_value = "csrss.exe"
        with patch.object(process_monitor.platform, "system", return_value="Windows"):
            protected = process_monitor.terminate_process(1235)
        self.assertEqual(protected["code"], "PROTECTED_PROCESS")
        self.process.terminate.assert_not_called()


class ProcessCommandAuthenticationTests(TestCase):
    def test_signature_is_bound_to_request_client_and_argument(self) -> None:
        signature = sign_process_command(
            "shared-secret",
            "request123",
            "PC-01",
            "TERMINATE_PROCESS",
            "4521",
        )

        self.assertTrue(
            verify_process_command(
                "shared-secret",
                "request123",
                "pc-01",
                "TERMINATE_PROCESS",
                "4521",
                signature,
            )
        )
        self.assertFalse(
            verify_process_command(
                "shared-secret",
                "request123",
                "PC-02",
                "TERMINATE_PROCESS",
                "4521",
                signature,
            )
        )
        self.assertFalse(
            verify_process_command(
                "shared-secret",
                "request123",
                "PC-01",
                "TERMINATE_PROCESS",
                "4522",
                signature,
            )
        )

    def test_client_rejects_unsigned_process_management_commands(self) -> None:
        fake_socket = Mock()
        fake_socket.recv.return_value = b"OK|COMMAND\n"

        with patch.dict(os.environ, {"MONITOR_ADMIN_TOKEN": "client-secret"}):
            with patch.object(tcp_client, "terminate_process") as terminate:
                result = tcp_client.TCPClient(
                    default_name="PC-01"
                )._answer_server_command(
                    fake_socket,
                    "COMMAND|request123|TERMINATE_PROCESS|4521|invalid",
                    "PC-01",
                )

        self.assertEqual(result["status"], "error")
        self.assertIn(
            b"COMMAND_ERROR|request123|UNAUTHORIZED\n",
            fake_socket.sendall.call_args.args[0],
        )
        terminate.assert_not_called()

    def test_client_executes_signed_command_once_and_rejects_replay(self) -> None:
        request_id = "request123"
        signature = sign_process_command(
            "client-secret",
            request_id,
            "PC-01",
            "GET_PROCESSES",
        )
        command = f"COMMAND|{request_id}|GET_PROCESSES|{signature}"
        fake_socket = Mock()
        fake_socket.recv.return_value = b"OK|COMMAND\n"
        tcp = tcp_client.TCPClient(default_name="PC-01")

        with patch.dict(os.environ, {"MONITOR_ADMIN_TOKEN": "client-secret"}):
            with patch.object(
                tcp_client,
                "collect_process_snapshot",
                return_value=[
                    {
                        "pid": 42,
                        "name": "worker.exe",
                        "username": "operator",
                        "cpu_percent": 1.0,
                        "memory_percent": 2.0,
                        "status": "running",
                    }
                ],
            ) as collect:
                first = tcp._answer_server_command(fake_socket, command, "PC-01")
                second = tcp._answer_server_command(fake_socket, command, "PC-01")

        self.assertEqual(first["command_status"], "complete")
        self.assertEqual(second["command_status"], "error")
        self.assertEqual(collect.call_count, 1)
        self.assertIn(
            b"COMMAND_ERROR|request123|REPLAYED\n",
            fake_socket.sendall.call_args_list[-1].args[0],
        )


class RemoteProcessCommandTests(TestCase):
    TOKEN = "process-management-test-token"
    ADDRESS_ONE = ("127.0.0.1", 31001)
    ADDRESS_TWO = ("127.0.0.1", 31002)
    PROCESS = {
        "pid": 4521,
        "name": "worker.exe",
        "username": "operator",
        "cpu_percent": 2.0,
        "memory_percent": 1.0,
        "status": "running",
    }

    def setUp(self) -> None:
        self.previous_token = server.ADMIN_TOKEN
        self.previous_clients = copy.deepcopy(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)
        self.previous_process_requests = copy.deepcopy(server.process_requests)
        self.previous_command_requests = copy.deepcopy(
            server.controlled_command_requests
        )
        self.previous_process_snapshots = copy.deepcopy(server.process_snapshots)
        server.ADMIN_TOKEN = self.TOKEN
        server.clients.clear()
        server.disconnected_clients.clear()
        server.process_requests.clear()
        server.controlled_command_requests.clear()
        server.process_snapshots.clear()
        for method, return_value in (
            ("register_client", True),
            ("update_heartbeat", True),
            ("update_status", True),
            ("add_process_termination_audit", 7001),
            ("complete_process_termination_audit", True),
            ("record_process_activity", True),
            ("get_process_activity", []),
        ):
            active_patch = patch.object(
                server.db_manager,
                method,
                return_value=return_value,
            )
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        server.ADMIN_TOKEN = self.previous_token
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)
        server.process_requests.clear()
        server.process_requests.update(self.previous_process_requests)
        server.controlled_command_requests.clear()
        server.controlled_command_requests.update(self.previous_command_requests)
        server.process_snapshots.clear()
        server.process_snapshots.update(self.previous_process_snapshots)

    def register(self, name: str, address: tuple[str, int] | None = None) -> None:
        address = address or self.ADDRESS_ONE
        response = server.handle_message(
            f"REGISTER|{name}|127.0.0.1|8888|"
            f"{server.PROCESS_LIST_CAPABILITY}|"
            f"{server.CONTROLLED_COMMANDS_CAPABILITY}|"
            f"{server.PROCESS_MANAGEMENT_CAPABILITY}",
            address,
        )
        self.assertEqual(response, "OK|REGISTERED")

    def admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.TOKEN}

    def queue(self, name: str, command: str, pid: int | None = None):
        body = {"command": command}
        if pid is not None:
            body["pid"] = pid
        return self.client.post(
            f"/api/clients/{name}/commands",
            headers=self.admin_headers(),
            json=body,
        )

    def test_get_processes_is_signed_and_returns_a_validated_list(self) -> None:
        self.register("process-one")
        queued = self.queue("process-one", "GET_PROCESSES")
        self.assertEqual(queued.status_code, 202)
        request_id = queued.get_json()["request"]["request_id"]

        command = server.handle_message(
            "HEARTBEAT|process-one",
            self.ADDRESS_ONE,
        )
        fields = command.split("|")
        self.assertEqual(fields[:3], ["COMMAND", request_id, "GET_PROCESSES"])
        self.assertTrue(
            verify_process_command(
                self.TOKEN,
                request_id,
                "process-one",
                "GET_PROCESSES",
                "",
                fields[3],
            )
        )
        response = server.handle_message(
            f"RESPONSE|{request_id}|{json.dumps([self.PROCESS])}",
            self.ADDRESS_ONE,
        )

        self.assertEqual(response, "OK|COMMAND")
        result = self.client.get(
            "/api/clients/process-one/commands",
            headers=self.admin_headers(),
        ).get_json()
        self.assertEqual(result["request"]["status"], "complete")
        self.assertEqual(result["request"]["result"], [self.PROCESS])
        self.assertEqual(
            result["request"]["process_snapshot_updated_at"],
            server.process_snapshots["process-one"]["updated_at"],
        )
        self.assertEqual(
            server.process_snapshots["process-one"]["processes"],
            [{**self.PROCESS, "activity_status": "RUNNING"}],
        )

        next_queued = self.queue("process-one", "GET_PROCESSES")
        self.assertEqual(
            next_queued.get_json()["process_snapshot"]["processes"],
            [{**self.PROCESS, "activity_status": "RUNNING"}],
        )

    def test_full_process_snapshot_exceeds_legacy_display_limit(self) -> None:
        self.register("full-snapshot", self.ADDRESS_ONE)
        processes = [
            {**self.PROCESS, "pid": pid, "name": f"process-{pid}.exe"}
            for pid in range(1, server.PROCESS_LIST_TOP_N + 11)
        ]
        queued = self.queue("full-snapshot", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|full-snapshot", self.ADDRESS_ONE)

        response = server.handle_message(
            f"RESPONSE|{queued['request_id']}|{json.dumps(processes)}",
            self.ADDRESS_ONE,
        )

        self.assertEqual(response, "OK|COMMAND")
        self.assertEqual(
            len(server.process_snapshots["full-snapshot"]["processes"]),
            server.PROCESS_LIST_TOP_N + 10,
        )

    def test_legacy_process_list_does_not_replace_activity_snapshot(self) -> None:
        self.register("process-one", self.ADDRESS_ONE)
        queued_snapshot = self.queue("process-one", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|process-one", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"RESPONSE|{queued_snapshot['request_id']}|{json.dumps([self.PROCESS])}",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )
        saved_snapshot = copy.deepcopy(server.process_snapshots["process-one"])

        legacy_request = self.client.post(
            "/api/clients/process-one/process-list",
            headers=self.admin_headers(),
        )
        request_id = legacy_request.get_json()["request"]["request_id"]
        command = server.handle_message("HEARTBEAT|process-one", self.ADDRESS_ONE)
        self.assertTrue(command.startswith(f"COMMAND|GET_PROCESS_LIST|{request_id}|"))
        self.assertEqual(
            server.handle_message(
                f"PROCESS_LIST|process-one|{request_id}|[]",
                self.ADDRESS_ONE,
            ),
            "OK|PROCESS_LIST",
        )

        self.assertEqual(server.process_snapshots["process-one"], saved_snapshot)
        server.db_manager.record_process_activity.assert_not_called()

    def test_process_snapshots_are_isolated_and_survive_failed_refreshes(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        self.register("pc-02", self.ADDRESS_TWO)
        first = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        second = self.queue("pc-02", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        server.handle_message("HEARTBEAT|pc-02", self.ADDRESS_TWO)

        for request, name, address, process in (
            (first, "pc-01", self.ADDRESS_ONE, self.PROCESS),
            (
                second,
                "pc-02",
                self.ADDRESS_TWO,
                {**self.PROCESS, "pid": 9876, "name": "other-worker.exe"},
            ),
        ):
            response = server.handle_message(
                f"RESPONSE|{request['request_id']}|{json.dumps([process])}",
                address,
            )
            self.assertEqual(response, "OK|COMMAND")
            payload = self.client.get(
                f"/api/clients/{name}/commands",
                headers=self.admin_headers(),
            ).get_json()
            self.assertEqual(payload["request"]["result"], [process])
            self.assertEqual(
                server.process_snapshots[name]["processes"],
                [{**process, "activity_status": "RUNNING"}],
            )

        self.assertNotEqual(
            server.process_snapshots["pc-01"]["processes"],
            server.process_snapshots["pc-02"]["processes"],
        )
        previous_snapshot = copy.deepcopy(server.process_snapshots["pc-01"])
        refresh = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"COMMAND_ERROR|{refresh['request_id']}|UNAVAILABLE",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )
        failed_refresh = self.client.get(
            "/api/clients/pc-01/commands",
            headers=self.admin_headers(),
        ).get_json()
        self.assertEqual(failed_refresh["request"]["status"], "error")
        self.assertEqual(
            failed_refresh["request"]["process_snapshot"]["processes"],
            previous_snapshot["processes"],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["last_successful_update"],
            previous_snapshot["last_successful_update"],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["monitoring_status"],
            "UNAVAILABLE",
        )

    def test_oversized_snapshot_marks_unavailable_without_closing_processes(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        baseline = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        server.handle_message(
            f"RESPONSE|{baseline['request_id']}|{json.dumps([self.PROCESS])}",
            self.ADDRESS_ONE,
        )
        last_good_processes = copy.deepcopy(
            server.process_snapshots["pc-01"]["processes"]
        )

        refresh = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        oversized = [
            {**self.PROCESS, "pid": pid, "name": f"process-{pid}"}
            for pid in range(1, server.PROCESS_SNAPSHOT_MAX_COUNT + 2)
        ]
        response = server.handle_message(
            f"RESPONSE|{refresh['request_id']}|{json.dumps(oversized)}",
            self.ADDRESS_ONE,
        )

        self.assertEqual(response, "ERROR|MALFORMED_COMMAND_RESPONSE")
        request = self.client.get(
            "/api/clients/pc-01/commands",
            headers=self.admin_headers(),
        ).get_json()["request"]
        self.assertEqual(request["status"], "error")
        self.assertEqual(request["error_code"], "MALFORMED_RESULT")
        self.assertEqual(
            server.process_snapshots["pc-01"]["processes"],
            last_good_processes,
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["monitoring_status"],
            "UNAVAILABLE",
        )
        server.db_manager.record_process_activity.assert_not_called()

    def test_snapshot_diffs_record_only_changes_for_the_matching_client(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        self.register("pc-02", self.ADDRESS_TWO)

        def send_snapshot(name: str, address: tuple[str, int], processes: list[dict]):
            queued = self.queue(name, "GET_PROCESSES").get_json()["request"]
            server.handle_message(f"HEARTBEAT|{name}", address)
            response = server.handle_message(
                f"RESPONSE|{queued['request_id']}|{json.dumps(processes)}",
                address,
            )
            self.assertEqual(response, "OK|COMMAND")

        activity_writer = server.db_manager.record_process_activity
        first = self.PROCESS
        started = {**self.PROCESS, "pid": 5000, "name": "notepad.exe"}
        other_client = {**self.PROCESS, "pid": 6000, "name": "calculator.exe"}

        send_snapshot("pc-01", self.ADDRESS_ONE, [first])
        send_snapshot("pc-02", self.ADDRESS_TWO, [self.PROCESS])
        send_snapshot("pc-01", self.ADDRESS_ONE, [first, started])
        send_snapshot("pc-01", self.ADDRESS_ONE, [first, started])
        send_snapshot("pc-01", self.ADDRESS_ONE, [first])
        send_snapshot("pc-02", self.ADDRESS_TWO, [self.PROCESS, other_client])

        self.assertEqual(
            activity_writer.call_args_list,
            [
                call("pc-01", [
                    {"pid": 5000, "name": "notepad.exe", "event_type": "STARTED"}
                ]),
                call("pc-01", [
                    {"pid": 5000, "name": "notepad.exe", "event_type": "STOPPED"}
                ]),
                call("pc-02", [
                    {"pid": 6000, "name": "calculator.exe", "event_type": "STARTED"}
                ]),
            ],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["processes"],
            [{**first, "activity_status": "RUNNING"}],
        )
        self.assertEqual(
            server.process_snapshots["pc-02"]["processes"][-1]["activity_status"],
            "NEW",
        )

    def test_initial_snapshot_is_a_baseline_and_collection_failure_preserves_it(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        queued = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"RESPONSE|{queued['request_id']}|{json.dumps([self.PROCESS])}",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )
        self.assertEqual(
            server.db_manager.record_process_activity.call_count,
            0,
        )
        previous = copy.deepcopy(server.process_snapshots["pc-01"])

        failed = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"COMMAND_ERROR|{failed['request_id']}|UNAVAILABLE",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["processes"],
            previous["processes"],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["last_successful_update"],
            previous["last_successful_update"],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["monitoring_status"],
            "UNAVAILABLE",
        )

    def test_activity_storage_failure_does_not_replace_the_last_good_snapshot(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)

        def send_snapshot(processes: list[dict]) -> str:
            queued = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
            server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
            return server.handle_message(
                f"RESPONSE|{queued['request_id']}|{json.dumps(processes)}",
                self.ADDRESS_ONE,
            )

        self.assertEqual(send_snapshot([self.PROCESS]), "OK|COMMAND")
        previous = copy.deepcopy(server.process_snapshots["pc-01"])
        server.db_manager.record_process_activity.return_value = False
        added = {**self.PROCESS, "pid": 5000, "name": "notepad.exe"}

        self.assertEqual(send_snapshot([self.PROCESS, added]), "ERROR|PROCESS_ACTIVITY_STORAGE")
        self.assertEqual(
            server.process_snapshots["pc-01"]["processes"],
            previous["processes"],
        )
        self.assertEqual(
            server.process_snapshots["pc-01"]["monitoring_status"],
            "UNAVAILABLE",
        )

    def test_activity_api_requires_admin_token_and_returns_client_scoped_events(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        server.db_manager.get_process_activity.return_value = [
            {
                "pid": 1234,
                "process_name": "notepad.exe",
                "event_type": "STARTED",
                "timestamp": "2026-09-29T08:52:31",
            }
        ]

        unauthorized = self.client.get("/api/clients/pc-01/process-activity")
        self.assertEqual(unauthorized.status_code, 401)
        response = self.client.get(
            "/api/clients/pc-01/process-activity",
            headers=self.admin_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["client"], "pc-01")
        self.assertEqual(response.get_json()["monitoring_status"], "UNAVAILABLE")
        self.assertEqual(response.get_json()["events"][0]["pid"], 1234)
        server.db_manager.get_process_activity.assert_called_once_with("pc-01", 50)

    def test_dashboard_contains_process_management_controls(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        for control in (
            "process-search",
            "process-status-filter",
            "process-sort",
            "refresh-processes",
            "terminate-process",
            "process-last-update",
            "process-monitoring-status",
            "process-activity",
            "Process Monitoring:",
            "STARTED",
            "10000",
        ):
            with self.subTest(control=control):
                self.assertIn(control, page)

    def test_termination_requires_admin_authorization_and_valid_pid(self) -> None:
        self.register("termination-node")
        unauthorized = self.client.post(
            "/api/clients/termination-node/commands",
            json={"command": "TERMINATE_PROCESS", "pid": 4521},
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(server.controlled_command_requests, {})

        invalid = self.queue("termination-node", "TERMINATE_PROCESS", True)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "INVALID_PID")
        server.db_manager.add_process_termination_audit.assert_called_once()
        server.db_manager.complete_process_termination_audit.assert_called_once()

    def test_termination_is_signed_executed_and_audited(self) -> None:
        self.register("termination-node")
        queued = self.queue("termination-node", "TERMINATE_PROCESS", 4521)
        self.assertEqual(queued.status_code, 202)
        request_id = queued.get_json()["request"]["request_id"]
        command = server.handle_message(
            "HEARTBEAT|termination-node",
            self.ADDRESS_ONE,
        )
        fields = command.split("|")
        self.assertEqual(fields[:4], ["COMMAND", request_id, "TERMINATE_PROCESS", "4521"])
        self.assertTrue(
            verify_process_command(
                self.TOKEN,
                request_id,
                "termination-node",
                "TERMINATE_PROCESS",
                "4521",
                fields[4],
            )
        )
        response_payload = {
            "status": "ok",
            "code": "PROCESS_TERMINATED",
            "pid": 4521,
            "name": "worker.exe",
            "message": "Process terminated successfully.",
        }
        response = server.handle_message(
            f"RESPONSE|{request_id}|{json.dumps(response_payload)}",
            self.ADDRESS_ONE,
        )

        self.assertEqual(response, "OK|COMMAND")
        server.db_manager.add_process_termination_audit.assert_called_once_with(
            "termination-node",
            "127.0.0.1",
            4521,
            None,
        )
        server.db_manager.complete_process_termination_audit.assert_called_once_with(
            7001,
            "worker.exe",
            "SUCCESS",
            None,
        )
        result = self.client.get(
            "/api/clients/termination-node/commands",
            headers=self.admin_headers(),
        ).get_json()["request"]
        self.assertEqual(result["result"]["name"], "worker.exe")
        self.assertEqual(result["audit_status"], "saved")

    def test_remote_termination_is_detected_as_a_stopped_process(self) -> None:
        self.register("termination-node")
        first_request = self.queue("termination-node", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|termination-node", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"RESPONSE|{first_request['request_id']}|{json.dumps([self.PROCESS])}",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )

        terminate = self.queue("termination-node", "TERMINATE_PROCESS", 4521)
        terminate_request = terminate.get_json()["request"]
        server.handle_message("HEARTBEAT|termination-node", self.ADDRESS_ONE)
        terminated = {
            "status": "ok",
            "code": "PROCESS_TERMINATED",
            "pid": 4521,
            "name": "worker.exe",
            "message": "Process terminated successfully.",
        }
        self.assertEqual(
            server.handle_message(
                f"RESPONSE|{terminate_request['request_id']}|{json.dumps(terminated)}",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )

        next_snapshot = self.queue("termination-node", "GET_PROCESSES").get_json()["request"]
        server.handle_message("HEARTBEAT|termination-node", self.ADDRESS_ONE)
        self.assertEqual(
            server.handle_message(
                f"RESPONSE|{next_snapshot['request_id']}|[]",
                self.ADDRESS_ONE,
            ),
            "OK|COMMAND",
        )
        server.db_manager.record_process_activity.assert_called_once_with(
            "termination-node",
            [{"pid": 4521, "name": "worker.exe", "event_type": "STOPPED"}],
        )

    def test_termination_timeout_is_recorded_in_audit(self) -> None:
        self.register("timeout-node")
        queued = self.queue("timeout-node", "TERMINATE_PROCESS", 4521)
        self.assertEqual(queued.status_code, 202)
        with server.state_lock:
            server.controlled_command_requests["timeout-node"]["expires_at"] = (
                server.time.monotonic() - 1
            )

        result = self.client.get(
            "/api/clients/timeout-node/commands",
            headers=self.admin_headers(),
        ).get_json()["request"]

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["audit_status"], "saved")
        server.db_manager.complete_process_termination_audit.assert_called_once_with(
            7001,
            None,
            "TIMEOUT",
            "CLIENT_TIMEOUT_OR_DISCONNECTED",
        )

    def test_new_commands_do_not_run_on_legacy_clients(self) -> None:
        response = server.handle_message(
            "REGISTER|legacy-node|127.0.0.1|8888|"
            f"{server.CONTROLLED_COMMANDS_CAPABILITY}",
            self.ADDRESS_ONE,
        )
        self.assertEqual(response, "OK|REGISTERED")
        queued = self.queue("legacy-node", "GET_PROCESSES")

        self.assertEqual(queued.status_code, 409)
        self.assertEqual(
            server.handle_message("HEARTBEAT|legacy-node", self.ADDRESS_ONE),
            "OK|HEARTBEAT",
        )

    def test_two_clients_receive_only_their_own_process_requests(self) -> None:
        self.register("pc-01", self.ADDRESS_ONE)
        self.register("pc-02", self.ADDRESS_TWO)
        first = self.queue("pc-01", "GET_PROCESSES").get_json()["request"]
        second = self.queue("pc-02", "GET_PROCESSES").get_json()["request"]

        first_command = server.handle_message("HEARTBEAT|pc-01", self.ADDRESS_ONE)
        second_command = server.handle_message("HEARTBEAT|pc-02", self.ADDRESS_TWO)

        self.assertIn(first["request_id"], first_command)
        self.assertIn(second["request_id"], second_command)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertTrue(
            verify_process_command(
                self.TOKEN,
                first["request_id"],
                "pc-01",
                "GET_PROCESSES",
                "",
                first_command.split("|")[3],
            )
        )
        self.assertTrue(
            verify_process_command(
                self.TOKEN,
                second["request_id"],
                "pc-02",
                "GET_PROCESSES",
                "",
                second_command.split("|")[3],
            )
        )

class AdminTokenConfigurationTests(TestCase):
    """Verify server behavior when MONITOR_ADMIN_TOKEN is / is not configured."""

    def setUp(self) -> None:
        from pathlib import Path
        self.Path = Path
        self.previous_token = server.ADMIN_TOKEN
        self.previous_clients = copy.deepcopy(server.clients)
        self.previous_disconnected = set(server.disconnected_clients)
        self.previous_command_requests = copy.deepcopy(server.controlled_command_requests)
        server.clients.clear()
        server.disconnected_clients.clear()
        server.controlled_command_requests.clear()
        for method, return_value in (
            ("register_client", True),
            ("update_heartbeat", True),
            ("add_process_termination_audit", 9001),
            ("complete_process_termination_audit", True),
        ):
            p = patch.object(server.db_manager, method, return_value=return_value)
            p.start()
            self.addCleanup(p.stop)
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        server.ADMIN_TOKEN = self.previous_token
        server.clients.clear()
        server.clients.update(self.previous_clients)
        server.disconnected_clients.clear()
        server.disconnected_clients.update(self.previous_disconnected)
        server.controlled_command_requests.clear()
        server.controlled_command_requests.update(self.previous_command_requests)

    def _register(self, name: str) -> None:
        response = server.handle_message(
            f"REGISTER|{name}|127.0.0.1|8888|"
            f"{server.PROCESS_LIST_CAPABILITY}|"
            f"{server.CONTROLLED_COMMANDS_CAPABILITY}|"
            f"{server.PROCESS_MANAGEMENT_CAPABILITY}",
            ("127.0.0.1", 40001),
        )
        self.assertEqual(response, "OK|REGISTERED")

    def test_health_reports_admin_enabled_when_token_configured(self) -> None:
        server.ADMIN_TOKEN = "test-secret-token"
        resp = self.client.get("/api/health")
        self.assertTrue(resp.get_json()["admin_disconnect_enabled"])

    def test_health_reports_admin_disabled_when_token_missing(self) -> None:
        server.ADMIN_TOKEN = ""
        resp = self.client.get("/api/health")
        self.assertFalse(resp.get_json()["admin_disconnect_enabled"])

    def test_missing_token_returns_503_for_admin_endpoints(self) -> None:
        server.ADMIN_TOKEN = ""
        self._register("config-test")
        resp = self.client.post(
            "/api/clients/config-test/commands",
            headers={"X-Admin-Token": "anything"},
            json={"command": "GET_PROCESSES"},
        )
        self.assertEqual(resp.status_code, 503)

    def test_invalid_token_returns_401(self) -> None:
        server.ADMIN_TOKEN = "real-secret"
        self._register("config-test")
        resp = self.client.post(
            "/api/clients/config-test/commands",
            headers={"X-Admin-Token": "wrong-secret"},
            json={"command": "GET_PROCESSES"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_authorizes_admin_operations(self) -> None:
        server.ADMIN_TOKEN = "real-secret"
        self._register("config-test")
        resp = self.client.post(
            "/api/clients/config-test/commands",
            headers={"X-Admin-Token": "real-secret"},
            json={"command": "GET_PROCESSES"},
        )
        self.assertEqual(resp.status_code, 202)

    def test_process_management_blocked_when_token_missing(self) -> None:
        server.ADMIN_TOKEN = ""
        self._register("config-test")
        # Even with valid auth header, if server token is empty,
        # _admin_token_error returns 503 before reaching the queue
        resp = self.client.post(
            "/api/clients/config-test/commands",
            headers={"X-Admin-Token": ""},
            json={"command": "TERMINATE_PROCESS", "pid": 1234},
        )
        self.assertEqual(resp.status_code, 503)

    def test_token_not_in_env_example(self) -> None:
        """Verify .env.example only contains a placeholder, not a real secret."""
        env_example = self.Path(__file__).resolve().parent.parent / ".env.example"
        content = env_example.read_text(encoding="utf-8")
        self.assertIn("MONITOR_ADMIN_TOKEN=", content)
        # Must contain a placeholder, not a real token (32+ chars of random)
        for line in content.splitlines():
            if line.strip().startswith("MONITOR_ADMIN_TOKEN="):
                value = line.split("=", 1)[1].strip()
                self.assertIn(
                    value,
                    {"", "your-random-admin-token-here", "your-random-secret-token"},
                    "env.example must contain a placeholder, not a real token",
                )

    def test_gitignore_excludes_env(self) -> None:
        """Verify .env is listed in .gitignore."""
        gitignore = self.Path(__file__).resolve().parent.parent / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn(".env", content)
