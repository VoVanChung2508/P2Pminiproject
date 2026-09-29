
import os
import logging
import re
import time
from functools import wraps
from threading import RLock
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from dotenv import dotenv_values


def load_project_environment(env_file: Optional[Path] = None) -> None:
    project_env = dotenv_values(
        env_file or Path(__file__).resolve().parent.parent / ".env"
    )
    for name, value in project_env.items():
        if value is not None and not os.environ.get(name, "").strip():
            os.environ[name] = value


load_project_environment()

try:
    import mysql.connector
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False


logger = logging.getLogger("DatabaseManager")
T = TypeVar("T")
MYSQL_RECONNECT_ATTEMPTS = 3
MYSQL_RECONNECT_DELAY_SECONDS = 0.25
TRANSIENT_MYSQL_ERRNOS = {1040, 1053, 1927, 2002, 2003, 2006, 2013, 2055}
PROCESS_ACTIVITY_HISTORY_LIMIT = 500


def _synchronized(method: Callable[..., T]) -> Callable[..., T]:
    @wraps(method)
    def wrapper(self: "DatabaseManager", *args: object, **kwargs: object) -> T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class DatabaseManager:

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        port: Optional[int] = None,
        create_database: Optional[bool] = None,
    ):
        self.host = host if host is not None else os.environ.get("MYSQL_HOST", "localhost")
        self.user = user if user is not None else os.environ.get("MYSQL_USER", "root")
        self.password = password if password is not None else os.environ.get("MYSQL_PASSWORD", "")
        self.database = database if database is not None else os.environ.get("MYSQL_DB", "network_monitor")
        self.port = port if port is not None else int(os.environ.get("MYSQL_PORT", "3306"))
        create_database_setting = os.environ.get("MYSQL_CREATE_DATABASE", "true")
        self.create_database = (
            create_database
            if create_database is not None
            else create_database_setting.strip().lower() in {"1", "true", "yes", "on"}
        )

        self.db_conn = None
        self.is_connected = False
        self._lock = RLock()
        self._needs_reconnect = False
        self._last_connection_error: Optional[Exception] = None

    # ============================================================
    # CONNECT MYSQL
    # ============================================================

    @_synchronized
    def connect(self) -> bool:
        self._discard_connection()
        self._last_connection_error = None
        for attempt in range(1, MYSQL_RECONNECT_ATTEMPTS + 1):
            if self._connect_once():
                self._needs_reconnect = False
                return True

            error = self._last_connection_error
            if (
                error is None
                or not self._is_connection_error(error)
                or attempt == MYSQL_RECONNECT_ATTEMPTS
            ):
                return False

            logger.warning(
                "MySQL connection attempt %s/%s failed (errno=%s); retrying.",
                attempt,
                MYSQL_RECONNECT_ATTEMPTS,
                getattr(error, "errno", None),
            )
            time.sleep(MYSQL_RECONNECT_DELAY_SECONDS * attempt)
            self._discard_connection(needs_reconnect=True)

        return False

    @_synchronized
    def _connect_once(self) -> bool:

        if not MYSQL_AVAILABLE:
            logger.error(
                "mysql-connector-python chưa được cài đặt; persistent MySQL storage is unavailable."
            )
            self.is_connected = False
            return False

        if not re.fullmatch(r"[A-Za-z0-9_]+", self.database):
            logger.error("MYSQL_DB must contain only letters, digits, and underscores.")
            self.is_connected = False
            return False

        if not self.create_database:
            return self._connect_existing_database()

        conn = None
        cursor = None
        try:

            # ----------------------------------------------------
            # BƯỚC 1: Kết nối MySQL Server
            # ----------------------------------------------------

            conn = mysql.connector.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                port=self.port
            )

            cursor = conn.cursor()

            # ----------------------------------------------------
            # BƯỚC 2: Tạo database nếu chưa tồn tại
            # ----------------------------------------------------

            cursor.execute(
                f"""
                CREATE DATABASE IF NOT EXISTS `{self.database}`
                CHARACTER SET utf8mb4
                COLLATE utf8mb4_unicode_ci
                """
            )

            conn.commit()

            cursor.close()
            cursor = None
            conn.close()
            conn = None

            # ----------------------------------------------------
            # BƯỚC 3: Kết nối trực tiếp vào database
            # ----------------------------------------------------

            self.db_conn = mysql.connector.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
                port=self.port,
                autocommit=False
            )

            # ----------------------------------------------------
            # BƯỚC 4: Tạo bảng
            # ----------------------------------------------------

            self._create_tables()

            self.is_connected = True

            logger.info(
                f"MySQL CONNECTED: "
                f"{self.database}@{self.host}:{self.port}"
            )

            return True

        except mysql.connector.Error as e:

            self._last_connection_error = e
            self.is_connected = False
            self._needs_reconnect = self._is_connection_error(e)
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error(
                        "Unable to close failed MySQL connection (%s).",
                        type(close_error).__name__,
                    )
                self.db_conn = None

            if e.errno == 1045:
                logger.error(
                    "MySQL authentication failed. Verify MYSQL_USER and "
                    "MYSQL_PASSWORD in the project's .env file or Windows environment "
                    "variables. Password values are not logged.",
                )
            else:
                logger.error(
                    "MySQL connection failed (errno=%s, sqlstate=%s); "
                    "server cannot persist data.",
                    getattr(e, "errno", None),
                    getattr(e, "sqlstate", None),
                )

            return False

        except Exception as e:

            self._last_connection_error = e
            self.is_connected = False
            self._needs_reconnect = False
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error(
                        "Unable to close failed MySQL connection (%s).",
                        type(close_error).__name__,
                    )
                self.db_conn = None

            logger.error(
                "MySQL initialization failed (%s); server cannot persist data.",
                type(e).__name__,
            )

            return False
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_error:
                    logger.error(
                        "Unable to close MySQL setup cursor (%s).",
                        type(close_error).__name__,
                    )
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_error:
                    logger.error(
                        "Unable to close MySQL setup connection (%s).",
                        type(close_error).__name__,
                    )

    def _connect_existing_database(self) -> bool:
        try:
            self.db_conn = mysql.connector.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
                port=self.port,
                autocommit=False,
            )
            self._create_tables()
            self.is_connected = True
            logger.info(
                "Connected to existing MySQL database: %s@%s:%s",
                self.database,
                self.host,
                self.port,
            )
            return True
        except Exception as error:
            self._last_connection_error = error
            self.is_connected = False
            self._needs_reconnect = self._is_connection_error(error)
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error(
                        "Unable to close failed MySQL connection (%s).",
                        type(close_error).__name__,
                    )
                self.db_conn = None
            if getattr(error, "errno", None) == 1045:
                logger.error(
                    "MySQL authentication failed. Verify MYSQL_USER and "
                    "MYSQL_PASSWORD in the project's .env file or Windows environment "
                    "variables. Password values are not logged.",
                )
            else:
                logger.error(
                    "Could not connect to existing MySQL database (errno=%s, "
                    "sqlstate=%s); persistent storage is unavailable.",
                    getattr(error, "errno", None),
                    getattr(error, "sqlstate", None),
                )
            return False

    # ============================================================
    # CREATE TABLES
    # ============================================================

    @_synchronized
    def _create_tables(self) -> None:

        if self.db_conn is None:
            raise RuntimeError(
                "Database connection chưa được thiết lập."
            )

        cursor = self.db_conn.cursor()

        try:

            # ----------------------------------------------------
            # CLIENTS
            # ----------------------------------------------------

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (

                    id INT AUTO_INCREMENT PRIMARY KEY,

                    client_key VARCHAR(100)
                        UNIQUE NOT NULL,

                    name VARCHAR(100)
                        NOT NULL,

                    ip VARCHAR(45)
                        NOT NULL,

                    cpu FLOAT DEFAULT 0,

                    ram FLOAT DEFAULT 0,

                    disk FLOAT DEFAULT 0,

                    network FLOAT DEFAULT 0,

                    upload_bytes_per_sec DOUBLE NULL,

                    download_bytes_per_sec DOUBLE NULL,

                    packets_sent BIGINT UNSIGNED NULL,

                    packets_recv BIGINT UNSIGNED NULL,

                    status VARCHAR(20)
                        DEFAULT 'ONLINE',

                    last_seen DATETIME,

                    registered_at DATETIME

                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4;
                """
            )

            # ----------------------------------------------------
            # HISTORY
            # ----------------------------------------------------

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS history (

                    id BIGINT AUTO_INCREMENT PRIMARY KEY,

                    client_key VARCHAR(100)
                        NOT NULL,

                    cpu FLOAT NOT NULL,

                    ram FLOAT NOT NULL,

                    disk FLOAT NOT NULL,

                    network FLOAT NOT NULL,

                    upload_bytes_per_sec DOUBLE NULL,

                    download_bytes_per_sec DOUBLE NULL,

                    packets_sent BIGINT UNSIGNED NULL,

                    packets_recv BIGINT UNSIGNED NULL,

                    timestamp DATETIME NOT NULL,

                    INDEX idx_client_time
                    (client_key, timestamp)

                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4;
                """
            )

            # ----------------------------------------------------
            # ALERTS
            # ----------------------------------------------------

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (

                    id BIGINT AUTO_INCREMENT PRIMARY KEY,

                    client_key VARCHAR(100)
                        NOT NULL,

                    client_name VARCHAR(100)
                        NOT NULL,

                    metric VARCHAR(20)
                        NOT NULL,

                    value FLOAT NOT NULL,

                    limit_val FLOAT NOT NULL,

                    timestamp DATETIME NOT NULL

                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4;
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS process_termination_audit (

                    id BIGINT AUTO_INCREMENT PRIMARY KEY,

                    client_key VARCHAR(100)
                        NOT NULL,

                    client_name VARCHAR(100)
                        NOT NULL,

                    client_ip VARCHAR(45) NULL,

                    pid BIGINT UNSIGNED NULL,

                    process_name VARCHAR(256) NULL,

                    action VARCHAR(32)
                        NOT NULL,

                    result VARCHAR(32)
                        NOT NULL,

                    error_reason VARCHAR(512) NULL,

                    requested_at DATETIME NOT NULL,

                    completed_at DATETIME NULL,

                    INDEX idx_process_audit_client_time
                    (client_key, requested_at)

                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4;
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS process_activity (

                    id BIGINT AUTO_INCREMENT PRIMARY KEY,

                    client_key VARCHAR(100)
                        NOT NULL,

                    client_name VARCHAR(100)
                        NOT NULL,

                    pid BIGINT UNSIGNED
                        NOT NULL,

                    process_name VARCHAR(256)
                        NOT NULL,

                    event_type VARCHAR(16)
                        NOT NULL,

                    timestamp DATETIME
                        NOT NULL,

                    INDEX idx_process_activity_client_time
                    (client_key, timestamp, id)

                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4;
                """
            )

            additive_columns = {
                "clients": {
                    "upload_bytes_per_sec": "DOUBLE NULL",
                    "download_bytes_per_sec": "DOUBLE NULL",
                    "packets_sent": "BIGINT UNSIGNED NULL",
                    "packets_recv": "BIGINT UNSIGNED NULL",
                },
                "history": {
                    "upload_bytes_per_sec": "DOUBLE NULL",
                    "download_bytes_per_sec": "DOUBLE NULL",
                    "packets_sent": "BIGINT UNSIGNED NULL",
                    "packets_recv": "BIGINT UNSIGNED NULL",
                },
            }
            for table_name, columns in additive_columns.items():
                for column_name, column_definition in columns.items():
                    cursor.execute(
                        """
                        SELECT 1
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = %s
                          AND TABLE_NAME = %s
                          AND COLUMN_NAME = %s
                        """,
                        (self.database, table_name, column_name),
                    )
                    if cursor.fetchone() is None:
                        cursor.execute(
                            f"ALTER TABLE `{table_name}` "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )

            self.db_conn.commit()

            logger.info(
                "Database tables đã được tạo/kiểm tra thành công."
            )

        except Exception:

            self.db_conn.rollback()
            raise

        finally:

            cursor.close()

    # ============================================================
    # CHECK CONNECTION
    # ============================================================

    @staticmethod
    def _is_connection_error(error: Exception) -> bool:
        if getattr(error, "errno", None) == 1045:
            return False
        if getattr(error, "errno", None) in TRANSIENT_MYSQL_ERRNOS:
            return True
        if not MYSQL_AVAILABLE:
            return False
        interface_error = getattr(mysql.connector, "InterfaceError", ())
        operational_error = getattr(mysql.connector, "OperationalError", ())
        return (
            isinstance(error, interface_error)
            or (
                getattr(error, "errno", None) is None
                and isinstance(error, operational_error)
            )
        )

    @staticmethod
    def _log_connection_error(error: Exception) -> None:
        if getattr(error, "errno", None) == 1045:
            logger.error(
                "MySQL authentication failed. Verify MYSQL_USER and MYSQL_PASSWORD "
                "in the project environment. Secret values are not logged."
            )
            return
        logger.error(
            "MySQL connection failed (errno=%s, sqlstate=%s, type=%s); "
            "persistent storage is unavailable.",
            getattr(error, "errno", None),
            getattr(error, "sqlstate", None),
            type(error).__name__,
        )

    def _discard_connection(self, needs_reconnect: bool = False) -> None:
        connection = self.db_conn
        self.db_conn = None
        self.is_connected = False
        self._needs_reconnect = needs_reconnect
        if connection is not None:
            try:
                connection.close()
            except Exception as error:
                logger.warning(
                    "Could not close MySQL connection (%s).",
                    type(error).__name__,
                )

    def _check_connection(self) -> bool:
        if not MYSQL_AVAILABLE:
            return False
        if not self.is_connected or self.db_conn is None:
            if self._needs_reconnect:
                return self.connect()
            return False

        cursor = None
        health_error = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            # The connection disables autocommit; end the health-check read transaction.
            self.db_conn.rollback()
        except Exception as error:
            health_error = error
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as error:
                    logger.warning(
                        "MySQL health-check cursor close failed (%s).",
                        type(error).__name__,
                    )

        if health_error is None:
            return True

        logger.warning(
            "MySQL SELECT 1 health check failed (errno=%s, sqlstate=%s, type=%s); "
            "reconnecting.",
            getattr(health_error, "errno", None),
            getattr(health_error, "sqlstate", None),
            type(health_error).__name__,
        )
        self._handle_operation_error("connection health check", health_error)
        return self.is_connected

    def _handle_operation_error(self, operation: str, error: Exception) -> None:
        connection_failed = self._is_connection_error(error)
        rollback_failed = False
        if self.db_conn is not None:
            try:
                self.db_conn.rollback()
            except Exception as rollback_error:
                logger.error(
                    "MySQL rollback after %s failed (%s).",
                    operation,
                    type(rollback_error).__name__,
                )
                rollback_failed = True
                connection_failed = True

        if connection_failed:
            logger.error(
                "MySQL %s failed due to a connection error (errno=%s, sqlstate=%s, "
                "type=%s); the operation will not be replayed.",
                operation,
                getattr(error, "errno", None),
                getattr(error, "sqlstate", None),
                type(error).__name__,
            )
            self._discard_connection(needs_reconnect=True)
            self.connect()
            return

        self._discard_connection(needs_reconnect=rollback_failed)
        logger.error(
            "MySQL %s failed (%s); transaction was rolled back when possible.",
            operation,
            type(error).__name__,
        )
        if rollback_failed:
            self.connect()

    def _close_cursor(self, cursor: Any) -> None:
        if cursor is None:
            return
        try:
            cursor.close()
        except Exception as error:
            logger.error(
                "MySQL cursor close failed (%s).",
                type(error).__name__,
            )
            if self._is_connection_error(error):
                self._handle_operation_error("cursor close", error)

    # ============================================================
    # REGISTER CLIENT
    # ============================================================

    @_synchronized
    def register_client(
        self,
        name: str,
        ip: str
    ) -> bool:

        if not self._check_connection():
            return False

        key = name.lower()

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        cursor = None
        try:
            cursor = self.db_conn.cursor()

            query = """
            INSERT INTO clients
            (
                client_key,
                name,
                ip,
                status,
                last_seen,
                registered_at
            )

            VALUES
            (
                %s,
                %s,
                %s,
                'ONLINE',
                %s,
                %s
            )

            ON DUPLICATE KEY UPDATE

                name = VALUES(name),
                ip = VALUES(ip),
                status = 'ONLINE',
                last_seen = VALUES(last_seen)
            """

            cursor.execute(
                query,
                (
                    key,
                    name,
                    ip,
                    now,
                    now
                )
            )

            self.db_conn.commit()

            logger.info(
                f"Client registered: {name} ({ip})"
            )

            return True

        except Exception as e:
            self._handle_operation_error("register_client", e)
            return False

        finally:
            self._close_cursor(cursor)

    # ============================================================
    # UPDATE METRICS
    # ============================================================

    @_synchronized
    def update_metrics(
        self,
        name: str,
        metrics: Dict[str, Any]
    ) -> bool:

        if not self._check_connection():
            return False

        key = name.lower()

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        cpu = float(metrics.get("cpu", 0.0))
        ram = float(metrics.get("ram", 0.0))
        disk = float(metrics.get("disk", 0.0))
        network = float(metrics.get("network", 0.0))
        upload_bytes_per_sec = metrics.get("upload_bytes_per_sec")
        download_bytes_per_sec = metrics.get("download_bytes_per_sec")
        packets_sent = metrics.get("packets_sent")
        packets_recv = metrics.get("packets_recv")

        cursor = None
        try:
            cursor = self.db_conn.cursor()

            # ----------------------------------------------------
            # UPDATE CLIENT CURRENT STATUS
            # ----------------------------------------------------

            update_query = """
            UPDATE clients

            SET
                cpu = %s,
                ram = %s,
                disk = %s,
                network = %s,
                upload_bytes_per_sec = %s,
                download_bytes_per_sec = %s,
                packets_sent = %s,
                packets_recv = %s,
                status = 'ONLINE',
                last_seen = %s

            WHERE client_key = %s
            """

            cursor.execute(
                update_query,
                (
                    cpu,
                    ram,
                    disk,
                    network,
                    upload_bytes_per_sec,
                    download_bytes_per_sec,
                    packets_sent,
                    packets_recv,
                    now,
                    key
                )
            )

            # ----------------------------------------------------
            # KIỂM TRA CLIENT CÓ TỒN TẠI KHÔNG
            # ----------------------------------------------------

            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT 1 FROM clients WHERE client_key = %s",
                    (key,),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError(f"Client '{name}' chưa được đăng ký.")

            # ----------------------------------------------------
            # INSERT HISTORY
            # ----------------------------------------------------

            history_query = """
            INSERT INTO history
            (
                client_key,
                cpu,
                ram,
                disk,
                network,
                upload_bytes_per_sec,
                download_bytes_per_sec,
                packets_sent,
                packets_recv,
                timestamp
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """

            cursor.execute(
                history_query,
                (
                    key,
                    cpu,
                    ram,
                    disk,
                    network,
                    upload_bytes_per_sec,
                    download_bytes_per_sec,
                    packets_sent,
                    packets_recv,
                    now
                )
            )

            # ----------------------------------------------------
            # COMMIT
            # ----------------------------------------------------

            self.db_conn.commit()

            logger.debug(
                f"Metrics saved to MySQL: "
                f"{name} | "
                f"CPU={cpu}% | "
                f"RAM={ram}% | "
                f"Disk={disk}% | "
                f"LegacyNetwork={network} | "
                f"UploadBps={upload_bytes_per_sec} | "
                f"DownloadBps={download_bytes_per_sec}"
            )

            return True

        except Exception as e:
            self._handle_operation_error("update_metrics", e)
            return False

        finally:
            self._close_cursor(cursor)

    # ============================================================
    # ADD ALERT
    # ============================================================

    @_synchronized
    def add_alert(
        self,
        client_name: str,
        metric: str,
        value: float,
        limit_val: float
    ) -> bool:

        if not self._check_connection():
            return False

        key = client_name.lower()

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        cursor = None
        try:
            cursor = self.db_conn.cursor()

            query = """
            INSERT INTO alerts
            (
                client_key,
                client_name,
                metric,
                value,
                limit_val,
                timestamp
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """

            cursor.execute(
                query,
                (
                    key,
                    client_name,
                    metric.upper(),
                    value,
                    limit_val,
                    now
                )
            )

            self.db_conn.commit()

            logger.info(
                f"Alert saved to MySQL: "
                f"{client_name} | "
                f"{metric}={value}"
            )

            return True

        except Exception as e:
            self._handle_operation_error("add_alert", e)
            return False

        finally:
            self._close_cursor(cursor)

    @_synchronized
    def record_process_activity(
        self,
        client_name: str,
        events: list[Dict[str, Any]],
    ) -> bool:
        if not events:
            return True
        if not self._check_connection():
            return False

        for event in events:
            if (
                not isinstance(event, dict)
                or set(event) != {"pid", "name", "event_type"}
                or type(event.get("pid")) is not int
                or not 1 <= event["pid"] <= 4_294_967_295
                or not isinstance(event.get("name"), str)
                or not event["name"]
                or len(event["name"]) > 256
                or not isinstance(event.get("event_type"), str)
                or event.get("event_type") not in {"STARTED", "STOPPED"}
            ):
                raise ValueError("Process activity event is invalid.")

        key = client_name.lower()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = None
        try:
            cursor = self.db_conn.cursor()
            for event in events:
                cursor.execute(
                    """
                    INSERT INTO process_activity
                    (
                        client_key,
                        client_name,
                        pid,
                        process_name,
                        event_type,
                        timestamp
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        key,
                        client_name,
                        event["pid"],
                        event["name"],
                        event["event_type"],
                        timestamp,
                    ),
                )
            cursor.execute(
                """
                DELETE FROM process_activity
                WHERE client_key = %s
                  AND id NOT IN (
                      SELECT id FROM (
                          SELECT id
                          FROM process_activity
                          WHERE client_key = %s
                          ORDER BY timestamp DESC, id DESC
                          LIMIT %s
                      ) AS retained_activity
                  )
                """,
                (key, key, PROCESS_ACTIVITY_HISTORY_LIMIT),
            )
            self.db_conn.commit()
            return True
        except Exception as error:
            self._handle_operation_error("record_process_activity", error)
            return False
        finally:
            self._close_cursor(cursor)

    @_synchronized
    def get_process_activity(
        self,
        client_name: str,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        if not 1 <= limit <= PROCESS_ACTIVITY_HISTORY_LIMIT:
            raise ValueError(
                "Process activity limit must be between 1 and "
                f"{PROCESS_ACTIVITY_HISTORY_LIMIT}."
            )
        rows = self._read_rows(
            "get_process_activity",
            """
            SELECT pid, process_name, event_type, timestamp
            FROM process_activity
            WHERE client_key = %s
            ORDER BY timestamp DESC, id DESC
            LIMIT %s
            """,
            (client_name.lower(), limit),
        )
        return [
            {
                "pid": int(row[0]),
                "process_name": row[1],
                "event_type": row[2],
                "timestamp": self._format_datetime(row[3]),
            }
            for row in rows
        ]

    @_synchronized
    def add_process_termination_audit(
        self,
        client_name: str,
        client_ip: str | None,
        pid: int | None,
        process_name: str | None = None,
    ) -> int | None:
        if not self._check_connection():
            return None

        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                INSERT INTO process_termination_audit
                (
                    client_key,
                    client_name,
                    client_ip,
                    pid,
                    process_name,
                    action,
                    result,
                    error_reason,
                    requested_at
                )
                VALUES (%s, %s, %s, %s, %s, 'TERMINATE_PROCESS', 'REQUESTED', NULL, %s)
                """,
                (
                    client_name.lower(),
                    client_name,
                    client_ip,
                    pid,
                    process_name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            audit_id = cursor.lastrowid
            self.db_conn.commit()
            return int(audit_id) if audit_id is not None else None
        except Exception as error:
            self._handle_operation_error("add_process_termination_audit", error)
            return None
        finally:
            self._close_cursor(cursor)

    @_synchronized
    def complete_process_termination_audit(
        self,
        audit_id: int,
        process_name: str | None,
        result: str,
        error_reason: str | None = None,
    ) -> bool:
        if not self._check_connection():
            return False
        if result not in {"SUCCESS", "FAILED", "TIMEOUT", "REJECTED"}:
            raise ValueError("Unsupported process termination audit result.")

        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                UPDATE process_termination_audit
                SET process_name = %s,
                    result = %s,
                    error_reason = %s,
                    completed_at = %s
                WHERE id = %s
                """,
                (
                    process_name,
                    result,
                    error_reason[:512] if error_reason else None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    audit_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Process termination audit record was not found.")
            self.db_conn.commit()
            return True
        except Exception as error:
            self._handle_operation_error("complete_process_termination_audit", error)
            return False
        finally:
            self._close_cursor(cursor)

    # ============================================================
    # UPDATE STATUS
    # ============================================================

    @_synchronized
    def update_status(
        self,
        name: str,
        status: str
    ) -> bool:

        if not self._check_connection():
            return False

        key = name.lower()

        cursor = None
        try:
            cursor = self.db_conn.cursor()

            query = """
            UPDATE clients

            SET status = %s

            WHERE client_key = %s
            """

            cursor.execute(
                query,
                (
                    status,
                    key
                )
            )

            self.db_conn.commit()

            logger.info(
                f"Client status updated: "
                f"{name} -> {status}"
            )

            return True

        except Exception as e:
            self._handle_operation_error("update_status", e)
            return False

        finally:
            self._close_cursor(cursor)

    @_synchronized
    def mark_all_clients_offline(self) -> bool:
        if not self._check_connection():
            return False
        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("UPDATE clients SET status = 'OFFLINE' WHERE status <> 'OFFLINE'")
            self.db_conn.commit()
            return True
        except Exception as error:
            self._handle_operation_error("mark_all_clients_offline", error)
            return False
        finally:
            self._close_cursor(cursor)

    @_synchronized
    def update_heartbeat(self, name: str) -> bool:
        if not self._check_connection():
            return False
        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                UPDATE clients
                SET status = 'ONLINE', last_seen = %s
                WHERE client_key = %s
                """,
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name.lower()),
            )
            self.db_conn.commit()
            return True
        except Exception as error:
            self._handle_operation_error("update_heartbeat", error)
            return False
        finally:
            self._close_cursor(cursor)

    def _read_rows(self, operation: str, query: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        for attempt in range(2):
            if not self._check_connection():
                raise RuntimeError(
                    "MySQL is unavailable; persistent data cannot be read."
                )
            cursor = None
            try:
                cursor = self.db_conn.cursor()
                cursor.execute(query, parameters)
                return list(cursor.fetchall())
            except Exception as error:
                can_retry = attempt == 0 and self._is_connection_error(error)
                self._handle_operation_error(operation, error)
                if can_retry and self.is_connected:
                    continue
                raise RuntimeError(
                    f"MySQL {operation} failed; persistent data cannot be read."
                ) from error
            finally:
                self._close_cursor(cursor)
        raise RuntimeError(
            "MySQL is unavailable; persistent data cannot be read."
        )

    @staticmethod
    def _format_datetime(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return str(value)

    @_synchronized
    def get_clients(self) -> list[Dict[str, Any]]:
        rows = self._read_rows(
            "get_clients",
            """
            SELECT name, ip, cpu, ram, disk, network,
                   upload_bytes_per_sec, download_bytes_per_sec,
                   packets_sent, packets_recv,
                   status, last_seen, registered_at
            FROM clients
            ORDER BY name
            """,
        )
        return [
            {
                "name": row[0],
                "ip": row[1],
                "cpu": float(row[2] or 0),
                "ram": float(row[3] or 0),
                "disk": float(row[4] or 0),
                "network": float(row[5] or 0),
                "upload_bytes_per_sec": (
                    float(row[6]) if row[6] is not None else None
                ),
                "download_bytes_per_sec": (
                    float(row[7]) if row[7] is not None else None
                ),
                "packets_sent": int(row[8]) if row[8] is not None else None,
                "packets_recv": int(row[9]) if row[9] is not None else None,
                "status": row[10],
                "last_seen": self._format_datetime(row[11]),
                "registered_at": self._format_datetime(row[12]),
            }
            for row in rows
        ]

    @_synchronized
    def get_history(self, name: str, limit: int = 120) -> list[Dict[str, Any]]:
        if not 1 <= limit <= 120:
            raise ValueError("History limit must be between 1 and 120.")
        rows = self._read_rows(
            "get_history",
            """
            SELECT cpu, ram, disk, network,
                   upload_bytes_per_sec, download_bytes_per_sec,
                   packets_sent, packets_recv, timestamp
            FROM history
            WHERE client_key = %s
            ORDER BY timestamp DESC, id DESC
            LIMIT %s
            """,
            (name.lower(), limit),
        )
        return [
            {
                "cpu": float(row[0]),
                "ram": float(row[1]),
                "disk": float(row[2]),
                "network": float(row[3]),
                "upload_bytes_per_sec": (
                    float(row[4]) if row[4] is not None else None
                ),
                "download_bytes_per_sec": (
                    float(row[5]) if row[5] is not None else None
                ),
                "packets_sent": int(row[6]) if row[6] is not None else None,
                "packets_recv": int(row[7]) if row[7] is not None else None,
                "timestamp": self._format_datetime(row[8]),
            }
            for row in reversed(rows)
        ]

    @_synchronized
    def get_alerts(self, limit: int = 100) -> list[Dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Alert limit must be between 1 and 100.")
        rows = self._read_rows(
            "get_alerts",
            """
            SELECT client_name, metric, value, limit_val, timestamp
            FROM alerts
            ORDER BY timestamp DESC, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            {
                "client": row[0],
                "metric": row[1],
                "value": float(row[2]),
                "limit": float(row[3]),
                "timestamp": self._format_datetime(row[4]),
            }
            for row in rows
        ]

    # ============================================================
    # CLOSE DATABASE
    # ============================================================

    @_synchronized
    def close(self) -> None:
        connection_was_open = self.db_conn is not None
        self._discard_connection()
        self._last_connection_error = None
        if connection_was_open:
            logger.info("MySQL connection đã đóng.")
