"""
Data/db.py — PostgreSQL Database Layer for Network Monitoring System
====================================================================
Cung cấp:
  - Thread-safe Connection Pool (psycopg2.pool.ThreadedConnectionPool)
  - Auto schema migration (tạo bảng nếu chưa có)
  - CRUD API cho Users, Devices, Metrics, Alerts, Thresholds

Cách dùng:
    from Data.db import init_database, create_user, authenticate_user, ...
    ok = init_database()   # gọi 1 lần khi khởi động server
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

try:
    from werkzeug.security import check_password_hash, generate_password_hash
    _WERKZEUG_AVAILABLE = True
except ImportError:
    import hashlib
    _WERKZEUG_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "db_config.json")

def _load_config() -> Dict[str, Any]:
    """Load DB config from db_config.json with env variable overrides."""
    defaults = {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "monitoring_db",
        "user": "postgres",
        "password": "postgres",
        "pool_min": 2,
        "pool_max": 10,
    }
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception as exc:
            logger.warning("Cannot read db_config.json: %s", exc)

    # Allow environment variable overrides
    if os.environ.get("PGHOST"):
        defaults["host"] = os.environ["PGHOST"]
    if os.environ.get("PGPORT"):
        defaults["port"] = int(os.environ["PGPORT"])
    if os.environ.get("PGDATABASE"):
        defaults["dbname"] = os.environ["PGDATABASE"]
    if os.environ.get("PGUSER"):
        defaults["user"] = os.environ["PGUSER"]
    if os.environ.get("PGPASSWORD"):
        defaults["password"] = os.environ["PGPASSWORD"]

    return defaults


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------
def _hash_password(raw_password: str) -> str:
    if _WERKZEUG_AVAILABLE:
        return generate_password_hash(raw_password)
    # Fallback: sha256 (not as secure, but functional)
    return "sha256$" + hashlib.sha256(raw_password.encode()).hexdigest()


def _verify_password(raw_password: str, password_hash: str) -> bool:
    if _WERKZEUG_AVAILABLE:
        return check_password_hash(password_hash, raw_password)
    # Fallback match
    return "sha256$" + hashlib.sha256(raw_password.encode()).hexdigest() == password_hash


# ---------------------------------------------------------------------------
# Schema SQL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(50)  UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name     VARCHAR(100) DEFAULT '',
    role          VARCHAR(20)  DEFAULT 'viewer'
                  CHECK (role IN ('admin', 'viewer')),
    is_active     BOOLEAN      DEFAULT TRUE,
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    last_login    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS devices (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(100) UNIQUE NOT NULL,
    ip            VARCHAR(50)  DEFAULT '',
    status        VARCHAR(20)  DEFAULT 'OFFLINE',
    last_seen     TIMESTAMPTZ  DEFAULT NOW(),
    registered_at TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id          BIGSERIAL    PRIMARY KEY,
    device_name VARCHAR(100) NOT NULL,
    cpu         NUMERIC(5,1) DEFAULT 0,
    ram         NUMERIC(5,1) DEFAULT 0,
    disk        NUMERIC(5,1) DEFAULT 0,
    network     NUMERIC(5,1) DEFAULT 0,
    recorded_at TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_metrics_device_time
    ON metrics_history(device_name, recorded_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id           SERIAL       PRIMARY KEY,
    device_name  VARCHAR(100) NOT NULL,
    metric_type  VARCHAR(20)  NOT NULL,
    value        NUMERIC(5,1) NOT NULL,
    threshold    NUMERIC(5,1) NOT NULL,
    triggered_at TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(triggered_at DESC);

CREATE TABLE IF NOT EXISTS alert_thresholds (
    metric    VARCHAR(20)  PRIMARY KEY,
    limit_pct NUMERIC(5,1) NOT NULL DEFAULT 80
);
INSERT INTO alert_thresholds(metric, limit_pct) VALUES
    ('cpu', 80), ('ram', 80), ('disk', 90), ('network', 95)
ON CONFLICT DO NOTHING;
"""


# ---------------------------------------------------------------------------
# Connection Pool Manager
# ---------------------------------------------------------------------------
class _DatabaseManager:
    """Thread-safe PostgreSQL connection pool singleton."""

    def __init__(self) -> None:
        self._pool: Optional[Any] = None
        self._lock = threading.Lock()
        self._available = False

    def init(self) -> bool:
        """
        Khởi tạo pool và chạy schema migration.
        Trả về True nếu kết nối thành công.
        """
        if not _PSYCOPG2_AVAILABLE:
            logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
            return False

        cfg = _load_config()
        with self._lock:
            try:
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=int(cfg["pool_min"]),
                    maxconn=int(cfg["pool_max"]),
                    host=cfg["host"],
                    port=int(cfg["port"]),
                    dbname=cfg["dbname"],
                    user=cfg["user"],
                    password=cfg["password"],
                    connect_timeout=5,
                )
                # Run schema migration
                conn = self._pool.getconn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(_SCHEMA_SQL)
                finally:
                    self._pool.putconn(conn)
                self._available = True
                logger.info(
                    "Connected to PostgreSQL %s:%s/%s",
                    cfg["host"], cfg["port"], cfg["dbname"],
                )
                return True
            except Exception as exc:
                logger.error("DB init failed: %s", exc)
                self._available = False
                return False

    @property
    def available(self) -> bool:
        return self._available

    @contextmanager
    def connection(self) -> Generator:
        """Context manager: lấy connection từ pool, auto-commit/rollback."""
        if not self._available or self._pool is None:
            raise RuntimeError("Database not available")
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)


_db = _DatabaseManager()


# ---------------------------------------------------------------------------
# Public Init API
# ---------------------------------------------------------------------------

def init_database() -> bool:
    """
    Khởi tạo kết nối database và tạo bảng schema.
    Gọi 1 lần duy nhất khi khởi động server.
    Trả về True nếu thành công.
    """
    ok = _db.init()
    if ok:
        # Seed default admin account if no users exist
        _seed_admin()
    return ok


def is_db_available() -> bool:
    """Kiểm tra database có đang kết nối không."""
    return _db.available


def _seed_admin() -> None:
    """Tạo tài khoản admin mặc định nếu chưa có user nào."""
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
                count = cur.fetchone()[0]
                if count == 0:
                    cur.execute(
                        """
                        INSERT INTO users (username, password_hash, full_name, role)
                        VALUES (%s, %s, %s, 'admin')
                        ON CONFLICT (username) DO NOTHING
                        """,
                        ("admin", _hash_password("Admin@123"), "System Administrator"),
                    )
                    logger.info("Default admin account created: admin / Admin@123")
    except Exception as exc:
        logger.warning("Could not seed admin account: %s", exc)


# ---------------------------------------------------------------------------
# User CRUD API
# ---------------------------------------------------------------------------

def create_user(
    username: str,
    password: str,
    full_name: str = "",
    role: str = "viewer",
) -> Dict[str, Any]:
    """
    Tạo tài khoản người dùng mới.
    Trả về {'success': True, 'user': {...}} hoặc {'success': False, 'error': '...'}.
    """
    if not _db.available:
        return {"success": False, "error": "Database not available"}
    if not username or not password:
        return {"success": False, "error": "Username and password are required"}
    if role not in ("admin", "viewer"):
        role = "viewer"

    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, full_name, role)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, username, full_name, role, created_at
                    """,
                    (username.strip(), _hash_password(password), full_name.strip(), role),
                )
                user = dict(cur.fetchone())
                user["created_at"] = user["created_at"].isoformat()
                return {"success": True, "user": user}
    except psycopg2.errors.UniqueViolation:
        return {"success": False, "error": f"Username '{username}' already exists"}
    except Exception as exc:
        logger.error("create_user error: %s", exc)
        return {"success": False, "error": str(exc)}


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Xác thực đăng nhập. Trả về dict thông tin user nếu thành công, None nếu sai.
    Cập nhật last_login khi đăng nhập thành công.
    """
    if not _db.available:
        return None
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, username, password_hash, full_name, role, is_active FROM users WHERE username = %s",
                    (username.strip(),),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                row = dict(row)
                if not row["is_active"]:
                    return None
                if not _verify_password(password, row["password_hash"]):
                    return None
                # Update last_login
                cur.execute(
                    "UPDATE users SET last_login = NOW() WHERE id = %s",
                    (row["id"],),
                )
                return {
                    "id": row["id"],
                    "username": row["username"],
                    "full_name": row["full_name"],
                    "role": row["role"],
                }
    except Exception as exc:
        logger.error("authenticate_user error: %s", exc)
        return None


def get_all_users() -> List[Dict[str, Any]]:
    """Lấy danh sách toàn bộ người dùng (không trả về password_hash)."""
    if not _db.available:
        return []
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, full_name, role, is_active,
                           created_at, last_login
                    FROM users ORDER BY created_at
                    """
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    r = dict(row)
                    r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
                    r["last_login"] = r["last_login"].isoformat() if r["last_login"] else None
                    result.append(r)
                return result
    except Exception as exc:
        logger.error("get_all_users error: %s", exc)
        return []


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Lấy thông tin người dùng theo username."""
    if not _db.available:
        return None
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, username, full_name, role, is_active, created_at FROM users WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                r = dict(row)
                r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
                return r
    except Exception as exc:
        logger.error("get_user_by_username error: %s", exc)
        return None


def update_user_role(username: str, new_role: str) -> bool:
    """Thay đổi vai trò người dùng. Trả về True nếu thành công."""
    if not _db.available:
        return False
    if new_role not in ("admin", "viewer"):
        return False
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET role = %s WHERE username = %s",
                    (new_role, username),
                )
                return cur.rowcount > 0
    except Exception as exc:
        logger.error("update_user_role error: %s", exc)
        return False


def toggle_user_active(username: str, is_active: bool) -> bool:
    """Kích hoạt / khóa tài khoản người dùng."""
    if not _db.available:
        return False
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_active = %s WHERE username = %s AND username != 'admin'",
                    (is_active, username),
                )
                return cur.rowcount > 0
    except Exception as exc:
        logger.error("toggle_user_active error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Device & Metrics API
# ---------------------------------------------------------------------------

def upsert_device(name: str, ip: str, status: str) -> None:
    """Thêm hoặc cập nhật thông tin thiết bị máy trạm."""
    if not _db.available:
        return
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO devices (name, ip, status, last_seen, registered_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (name) DO UPDATE
                        SET ip        = EXCLUDED.ip,
                            status    = EXCLUDED.status,
                            last_seen = NOW()
                    """,
                    (name, ip, status),
                )
    except Exception as exc:
        logger.warning("upsert_device error: %s", exc)


def update_device_status(name: str, status: str) -> None:
    """Cập nhật trạng thái thiết bị (ONLINE / OFFLINE)."""
    if not _db.available:
        return
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE devices SET status = %s, last_seen = NOW() WHERE name = %s",
                    (status, name),
                )
    except Exception as exc:
        logger.warning("update_device_status error: %s", exc)


def record_metric(
    device_name: str,
    cpu: float,
    ram: float,
    disk: float,
    network: float,
) -> None:
    """Ghi mẫu đo tài nguyên vào metrics_history."""
    if not _db.available:
        return
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO metrics_history (device_name, cpu, ram, disk, network)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (device_name, round(cpu, 1), round(ram, 1), round(disk, 1), round(network, 1)),
                )
    except Exception as exc:
        logger.warning("record_metric error: %s", exc)


def record_alert(
    device_name: str,
    metric_type: str,
    value: float,
    threshold: float,
) -> None:
    """Ghi cảnh báo vượt ngưỡng vào bảng alerts."""
    if not _db.available:
        return
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alerts (device_name, metric_type, value, threshold)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (device_name, metric_type.upper(), round(value, 1), round(threshold, 1)),
                )
    except Exception as exc:
        logger.warning("record_alert error: %s", exc)


def get_recent_history(device_name: str, limit: int = 120) -> List[Dict[str, Any]]:
    """
    Lấy lịch sử đo đạc gần nhất của một thiết bị.
    Trả về list các dict {'cpu', 'ram', 'disk', 'network', 'timestamp'}.
    """
    if not _db.available:
        return []
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT cpu, ram, disk, network,
                           recorded_at AS timestamp
                    FROM metrics_history
                    WHERE device_name = %s
                    ORDER BY recorded_at DESC
                    LIMIT %s
                    """,
                    (device_name, limit),
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    r = dict(row)
                    r["cpu"] = float(r["cpu"])
                    r["ram"] = float(r["ram"])
                    r["disk"] = float(r["disk"])
                    r["network"] = float(r["network"])
                    r["timestamp"] = r["timestamp"].isoformat(timespec="seconds")
                    result.append(r)
                return list(reversed(result))
    except Exception as exc:
        logger.error("get_recent_history error: %s", exc)
        return []


def get_recent_alerts(limit: int = 50) -> List[Dict[str, Any]]:
    """Lấy danh sách cảnh báo gần nhất từ database."""
    if not _db.available:
        return []
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT device_name AS client, metric_type AS metric,
                           value, threshold AS "limit", triggered_at AS timestamp
                    FROM alerts
                    ORDER BY triggered_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    r = dict(row)
                    r["value"] = float(r["value"])
                    r["limit"] = float(r["limit"])
                    r["timestamp"] = r["timestamp"].isoformat(timespec="seconds")
                    result.append(r)
                return result
    except Exception as exc:
        logger.error("get_recent_alerts error: %s", exc)
        return []


def get_all_devices() -> List[Dict[str, Any]]:
    """Lấy danh sách tất cả thiết bị đã từng đăng ký."""
    if not _db.available:
        return []
    try:
        with _db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT name, ip, status, last_seen, registered_at
                    FROM devices ORDER BY last_seen DESC
                    """
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    r = dict(row)
                    r["last_seen"] = r["last_seen"].isoformat(timespec="seconds") if r["last_seen"] else None
                    r["registered_at"] = r["registered_at"].isoformat(timespec="seconds") if r["registered_at"] else None
                    result.append(r)
                return result
    except Exception as exc:
        logger.error("get_all_devices error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Threshold API
# ---------------------------------------------------------------------------

def get_thresholds() -> Dict[str, float]:
    """Lấy ngưỡng cảnh báo hiện tại. Trả về {'cpu': 80.0, 'ram': 80.0, ...}."""
    defaults = {"cpu": 80.0, "ram": 80.0, "disk": 90.0, "network": 95.0}
    if not _db.available:
        return defaults
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metric, limit_pct FROM alert_thresholds")
                rows = cur.fetchall()
                result = dict(defaults)
                for metric, limit_pct in rows:
                    result[metric] = float(limit_pct)
                return result
    except Exception as exc:
        logger.error("get_thresholds error: %s", exc)
        return defaults


def update_threshold(metric: str, value: float) -> bool:
    """Cập nhật ngưỡng cảnh báo cho một metric. Trả về True nếu thành công."""
    if not _db.available:
        return False
    if metric not in ("cpu", "ram", "disk", "network"):
        return False
    if not 0 < value <= 100:
        return False
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alert_thresholds (metric, limit_pct)
                    VALUES (%s, %s)
                    ON CONFLICT (metric) DO UPDATE SET limit_pct = EXCLUDED.limit_pct
                    """,
                    (metric, round(value, 1)),
                )
                return True
    except Exception as exc:
        logger.error("update_threshold error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def export_metrics_csv(device_name: str, limit: int = 1000) -> str:
    """
    Xuất lịch sử đo đạc của thiết bị thành chuỗi CSV.
    Trả về chuỗi CSV UTF-8 BOM (để Excel đọc được tiếng Việt).
    """
    if not _db.available:
        return "timestamp,cpu,ram,disk,network\n"
    try:
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT recorded_at, cpu, ram, disk, network
                    FROM metrics_history
                    WHERE device_name = %s
                    ORDER BY recorded_at ASC
                    LIMIT %s
                    """,
                    (device_name, limit),
                )
                rows = cur.fetchall()
        lines = ["\ufeffTimestamp,CPU (%),RAM (%),Disk (%),Network (%)"]
        for ts, cpu, ram, disk, net in rows:
            lines.append(f"{ts.isoformat(timespec='seconds')},{cpu},{ram},{disk},{net}")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("export_metrics_csv error: %s", exc)
        return "timestamp,cpu,ram,disk,network\n"