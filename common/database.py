
import os
import logging
import re
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

    # ============================================================
    # CONNECT MYSQL
    # ============================================================

    @_synchronized
    def connect(self) -> bool:

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

            self.is_connected = False
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error("Unable to close failed MySQL connection: %s", close_error)
                self.db_conn = None

            if e.errno == 1045:
                logger.error(
                    "MySQL authentication failed for user '%s'. Verify MYSQL_USER and "
                    "MYSQL_PASSWORD in the project's .env file or Windows environment "
                    "variables. Password values are not logged.",
                    self.user,
                )
            else:
                logger.error(
                    f"MySQL connection failed; server cannot persist data: {e}"
                )

            return False

        except Exception as e:

            self.is_connected = False
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error("Unable to close failed MySQL connection: %s", close_error)
                self.db_conn = None

            logger.error(
                f"MySQL initialization failed; server cannot persist data: {e}"
            )

            return False
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_error:
                    logger.error("Unable to close MySQL setup cursor: %s", close_error)
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_error:
                    logger.error("Unable to close MySQL setup connection: %s", close_error)

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
            self.is_connected = False
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception as close_error:
                    logger.error("Unable to close failed MySQL connection: %s", close_error)
                self.db_conn = None
            if getattr(error, "errno", None) == 1045:
                logger.error(
                    "MySQL authentication failed for user '%s'. Verify MYSQL_USER and "
                    "MYSQL_PASSWORD in the project's .env file or Windows environment "
                    "variables. Password values are not logged.",
                    self.user,
                )
            else:
                logger.error(
                    "Could not connect to existing MySQL database; persistent storage "
                    "is unavailable: %s",
                    error,
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

    def _check_connection(self) -> bool:

        if not self.is_connected or self.db_conn is None:
            return False
        return True

    def _handle_operation_error(self, operation: str, error: Exception) -> None:
        self.is_connected = False
        if self.db_conn is not None:
            try:
                self.db_conn.rollback()
            except Exception as rollback_error:
                logger.error("MySQL rollback after %s failed: %s", operation, rollback_error)
        logger.error("MySQL %s failed; persistent storage is unavailable: %s", operation, error)

    def _close_cursor(self, cursor: Any) -> None:
        if cursor is None:
            return
        try:
            cursor.close()
        except Exception as error:
            self.is_connected = False
            logger.error("MySQL cursor close failed; persistent storage is unavailable: %s", error)

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
        metrics: Dict[str, float]
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
                history_query,
                (
                    key,
                    cpu,
                    ram,
                    disk,
                    network,
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
                f"Network={network}"
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
        if not self._check_connection():
            raise RuntimeError("MySQL is unavailable; persistent data cannot be read.")
        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(query, parameters)
            return list(cursor.fetchall())
        except Exception as error:
            self._handle_operation_error(operation, error)
            raise RuntimeError(f"MySQL {operation} failed; persistent data cannot be read.") from error
        finally:
            self._close_cursor(cursor)

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
            SELECT name, ip, cpu, ram, disk, network, status, last_seen, registered_at
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
                "status": row[6],
                "last_seen": self._format_datetime(row[7]),
                "registered_at": self._format_datetime(row[8]),
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
            SELECT cpu, ram, disk, network, timestamp
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
                "timestamp": self._format_datetime(row[4]),
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

        if self.db_conn is not None:

            try:

                self.db_conn.close()

            except Exception as e:

                logger.error(
                    f"Lỗi đóng MySQL connection: {e}"
                )

            finally:

                self.db_conn = None
                self.is_connected = False

                logger.info(
                    "MySQL connection đã đóng."
                )
