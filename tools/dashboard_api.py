#!/usr/bin/env python3
"""
TruePeopleSearch 可视化面板 — 后端 API (Flask)

依赖：pip install flask mysql-connector-python

启动：
  python3 dashboard_api.py --port 5000

然后浏览器打开 http://localhost:5000
"""

import argparse
import ast
import base64
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, g, jsonify, make_response, request, send_file
import mysql.connector
from werkzeug.exceptions import HTTPException

# 仓库 scripts/（tps_metrics / tps_queue）
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
_SCRIPTS_DIR = os.path.normpath(_SCRIPTS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

app = Flask(__name__, static_folder=None)

# ============================================================
# TiDB / Redis 连接配置
# ============================================================
def _config_port(environ, name, default):
    """Reject invalid settings without including their (possibly secret) values."""
    try:
        value = int(environ.get(name, default))
        if 1 <= value <= 65535:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError(f"{name} must be an integer from 1 to 65535") from None


def _config_text(environ, name, default):
    value = environ.get(name, default)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be non-empty text without control characters")
    return value


def _loopback_host(value, name):
    """Accept only fixed local bind addresses; resolve localhost locally."""
    if isinstance(value, str) and value.lower() == "localhost":
        return "127.0.0.1"
    if value in ("127.0.0.1", "::1"):
        return value
    raise ValueError(f"{name} must be 127.0.0.1, ::1, or localhost") from None


def _load_runtime_config(environ):
    database = {
        "host": _config_text(environ, "TPS_DB_HOST", "127.0.0.1"),
        "port": _config_port(environ, "TPS_DB_PORT", 4000),
        "user": _config_text(environ, "TPS_DB_USER", "root"),
        "password": environ.get("TPS_DB_PASSWORD", ""),
        "database": _config_text(environ, "TPS_DB_NAME", "people_search"),
        "autocommit": True,
    }
    redis = {
        "host": _config_text(environ, "TPS_REDIS_HOST", "127.0.0.1"),
        "port": _config_port(environ, "TPS_REDIS_PORT", 6379),
    }
    dashboard = {
        "host": _loopback_host(environ.get("TPS_DASHBOARD_HOST", "127.0.0.1"), "TPS_DASHBOARD_HOST"),
        "port": _config_port(environ, "TPS_DASHBOARD_PORT", 5001),
    }
    return database, redis, dashboard


TIDB_CONFIG, REDIS_CONFIG, DASHBOARD_CONFIG = _load_runtime_config(os.environ)
READINESS_TIMEOUT_SEC = 3
DB_CONNECT_TIMEOUT_SEC = 3
DB_IO_TIMEOUT_SEC = 30
PUBLIC_INTERNAL_ERROR = "本地服务暂时不可用"
_BACKGROUND_DB_SOURCE = Path(_SCRIPTS_DIR) / "scrape_to_tidb.py"
_BACKGROUND_DB_KEYS = ("host", "port", "user", "password", "database")
PROXY_TEST_URL = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60"
PROXY_TEST_MIN_TIMEOUT_SEC = 3
PROXY_TEST_MAX_TIMEOUT_SEC = 20
PROXY_TEST_MAX_RESPONSE_BYTES = 256 * 1024
REQUIRED_LOCAL_TABLES = (
    "persons", "aliases", "current_addresses", "previous_addresses",
    "phone_numbers", "email_addresses", "relatives", "associates", "stats_snapshot",
)
# One zero-row statement checks schema and SELECT privileges without reading
# stored records or multiplying the network timeout by the number of tables.
LOCAL_SCHEMA_CHECK_SQL = (
    "SELECT 1 FROM "
    + " CROSS JOIN ".join(f"`{table}`" for table in REQUIRED_LOCAL_TABLES)
    + " LIMIT 0"
)


class ReadinessDriverUnsupported(RuntimeError):
    code = "mysql_connector_9_2_required"

    def __init__(self):
        super().__init__("mysql-connector-python >= 9.2 is required for bounded database operations")

STATS_KEYS = (
    "persons",
    "phones",
    "emails",
    "prev_addr",
    "aliases",
)

CURSOR_SEP = "\x1f"
STATS_MAX_AGE_SEC = 60
POOL_SIZE = 5

_pool = None
_pool_lock = threading.Lock()
_has_person_counts = True
_redis_client = None
_redis_lock = threading.Lock()


def _mysql_errno(exc):
    return getattr(exc, "errno", None)


def _is_unknown_table(exc):
    if _mysql_errno(exc) == 1146:
        return True
    msg = str(exc).lower()
    return "doesn't exist" in msg or "does not exist" in msg


def _is_unknown_column(exc):
    if _mysql_errno(exc) == 1054:
        return True
    return "unknown column" in str(exc).lower()


def _as_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _background_database_target():
    """Read the actual worker's literal target without importing the scraper."""
    tree = ast.parse(_BACKGROUND_DB_SOURCE.read_text(encoding="utf-8"))
    assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "TIDB_CONFIG"
    ]
    references = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "TIDB_CONFIG"]
    getters = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_db"]
    if len(assignments) != 1 or len(references) != 2 or len(getters) != 1:
        raise ValueError("background database target is not statically verifiable")
    getter_body = getters[0].body
    result = getter_body[-1] if getter_body else None
    call = result.value if isinstance(result, ast.Return) else None
    if not (
        len(getter_body) == 2
        and isinstance(getter_body[0], ast.Expr)
        and isinstance(getter_body[0].value, ast.Constant)
        and isinstance(getter_body[0].value.value, str)
        and isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "connect"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "connector"
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "mysql"
        and not call.args
        and len(call.keywords) == 1
        and call.keywords[0].arg is None
        and isinstance(call.keywords[0].value, ast.Name)
        and call.keywords[0].value.id == "TIDB_CONFIG"
    ):
        raise ValueError("background database connection is not statically verifiable")
    target = ast.literal_eval(assignments[0].value)
    if not isinstance(target, dict) or any(key not in target for key in _BACKGROUND_DB_KEYS):
        raise ValueError("background database target is incomplete")
    if any(not isinstance(target[key], str) for key in ("host", "user", "password", "database")):
        raise ValueError("background database target has an invalid type")
    if type(target["port"]) is not int or not 1 <= target["port"] <= 65535:
        raise ValueError("background database port is invalid")
    return target


def _background_target_state(role):
    """Use the current environment inherited by children; fail closed on uncertainty."""
    try:
        redis_target = {
            "host": _config_text(os.environ, "REDIS_HOST", "127.0.0.1"),
            "port": _config_port(os.environ, "REDIS_PORT", 6379),
        }
        if redis_target != REDIS_CONFIG:
            return "mismatch"
        if role in ("worker", "cluster"):
            database_target = _background_database_target()
            if any(database_target[key] != TIDB_CONFIG[key] for key in _BACKGROUND_DB_KEYS):
                return "mismatch"
        elif role != "discover":
            return "unverifiable"
        return "match"
    except Exception:
        return "unverifiable"


def _start_target_guard(role):
    state = _background_target_state(role)
    if state == "match":
        return None
    if state == "mismatch":
        return jsonify({"ok": False, "code": "background_target_mismatch", "error": "面板与后台目标配置不一致，已拒绝启动"}), 409
    return jsonify({"ok": False, "code": "background_target_unverifiable", "error": "无法确认后台目标配置，已拒绝启动"}), 503


def _get_pool():
    """进程内小连接池；不可用则退回单连接。"""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            from mysql.connector import pooling
            _pool = pooling.MySQLConnectionPool(
                pool_name="dashboard",
                pool_size=POOL_SIZE,
                pool_reset_session=True,
                **_operational_mysql_options(),
            )
        except Exception:
            _pool = False
    return _pool


def get_db():
    """请求内复用同一连接；请求结束归还/关闭。"""
    db = g.get("db")
    if db is not None:
        try:
            db.ping(reconnect=True, attempts=1, delay=0)
            return db
        except Exception:
            try:
                db.close()
            except Exception:
                pass
            g.pop("db", None)

    pool = _get_pool()
    if pool:
        conn = pool.get_connection()
    else:
        conn = mysql.connector.connect(**_operational_mysql_options())
    g.db = conn
    return conn


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop("db", None)
    if db is None:
        return
    try:
        db.close()
    except Exception:
        pass


def query(sql, params=None):
    """执行查询，返回 dict 列表（复用请求内连接）。"""
    db = get_db()
    cur = db.cursor(dictionary=True, buffered=True)
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        cur.close()


def query_one(sql, params=None):
    """查询单条（复用请求内连接）。"""
    db = get_db()
    cur = db.cursor(dictionary=True, buffered=True)
    try:
        cur.execute(sql, params or ())
        return cur.fetchone()
    finally:
        cur.close()


def try_set_tiflash_read():
    """聚合查询优先 TiFlash；非 TiDB / 无副本则忽略。"""
    try:
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute("SET SESSION tidb_isolation_read_engines = 'tiflash,tikv'")
        finally:
            cur.close()
    except Exception:
        pass


def encode_cursor(full_name, person_id):
    raw = "{}{}{}".format(full_name or "", CURSOR_SEP, person_id or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor):
    if not cursor:
        raise ValueError("empty cursor")
    pad = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(cursor + pad).decode("utf-8")
    name, person_id = raw.split(CURSOR_SEP, 1)
    return name, person_id


def _stats_payload(row):
    return {key: _as_int(row.get(key)) for key in STATS_KEYS}


def _empty_metrics():
    try:
        from tps_metrics import BUCKETS, LATENCY_METRICS
    except Exception:
        BUCKETS = (
            "success", "empty", "http_4xx", "rate_limit", "cf_fail", "parse_fail",
            "write_fail", "dedup_hit", "retry", "dlq",
        )
        LATENCY_METRICS = ("scrape_ms", "write_ms", "queue_wait_ms", "cf_solve_ms")
    return {
        "counters": {bucket: 0 for bucket in BUCKETS},
        "latency": {
            metric: {"count": 0, "sum": 0.0, "avg": 0.0}
            for metric in LATENCY_METRICS
        },
        "ts": int(time.time()),
    }


def get_redis():
    """Redis 不可用则返回 None，不抛给路由。"""
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None

    try:
        import redis as redis_lib
    except ImportError:
        return None

    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            client = redis_lib.Redis(
                host=REDIS_CONFIG["host"],
                port=REDIS_CONFIG["port"],
                socket_connect_timeout=0.4,
                socket_timeout=0.8,
            )
            client.ping()
            _redis_client = client
            return client
        except Exception:
            return None


def _persons_select_cols(use_counts):
    if use_counts:
        return """
            p.person_id, p.full_name, p.age, p.birth_year,
            p.current_city, p.current_state, p.marital_status,
            p.phone_count, p.email_count, p.prev_addr_count
        """
    return """
            p.person_id, p.full_name, p.age, p.birth_year,
            p.current_city, p.current_state, p.marital_status,
            (SELECT COUNT(*) FROM phone_numbers WHERE person_id = p.person_id) as phone_count,
            (SELECT COUNT(*) FROM email_addresses WHERE person_id = p.person_id) as email_count,
            (SELECT COUNT(*) FROM previous_addresses WHERE person_id = p.person_id) as prev_addr_count
    """


def _load_stats():
    """库内档案实时计数 + 切片覆盖。不再读 relatives / associates / 旧 snapshot。"""
    try:
        row = query_one(
            """
            SELECT
                COUNT(*) AS persons,
                COALESCE(SUM(phone_count), 0) AS phones,
                COALESCE(SUM(email_count), 0) AS emails,
                COALESCE(SUM(prev_addr_count), 0) AS prev_addr,
                COALESCE(SUM(alias_count), 0) AS aliases
            FROM persons
            """
        )
        payload = _stats_payload(row or {})
    except Exception as exc:
        if not _is_unknown_column(exc):
            raise
        row = query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM persons) AS persons,
                (SELECT COUNT(*) FROM phone_numbers) AS phones,
                (SELECT COUNT(*) FROM email_addresses) AS emails,
                (SELECT COUNT(*) FROM previous_addresses) AS prev_addr,
                (SELECT COUNT(*) FROM aliases) AS aliases
            """
        )
        payload = _stats_payload(row or {})

    r = get_redis()
    if r is not None:
        try:
            from tps_coverage import coverage_snapshot, scale_snapshot
            from tps_queue import queue_stats
            payload["coverage"] = coverage_snapshot(r)
            payload["queue"] = queue_stats(r)
            payload["scale"] = scale_snapshot(r, persons=payload.get("persons") or 0, use_pages=False)
        except Exception:
            payload["coverage"] = {}
            payload["queue"] = {}
            payload["scale"] = {}
    else:
        payload["coverage"] = {}
        payload["queue"] = {}
        payload["scale"] = {}
    return payload


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    """返回前端页面"""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    resp = make_response(send_file(html_path))
    resp.headers["Cache-Control"] = "no-store"
    return resp


_DASHBOARD_ASSETS = {
    "/assets/dashboard-runtime.js": ("dashboard-runtime.js", "text/javascript"),
    "/assets/dashboard-app.js": ("dashboard-app.js", "text/javascript"),
    "/assets/dashboard.css": ("dashboard.css", "text/css"),
    "/dashboard-runtime.js": ("dashboard-runtime.js", "text/javascript"),
    "/dashboard-app.js": ("dashboard-app.js", "text/javascript"),
    "/dashboard.css": ("dashboard.css", "text/css"),
}


@app.route("/assets/dashboard-runtime.js", methods=["GET"])
@app.route("/assets/dashboard-app.js", methods=["GET"])
@app.route("/assets/dashboard.css", methods=["GET"])
@app.route("/dashboard-runtime.js", methods=["GET"])
@app.route("/dashboard-app.js", methods=["GET"])
@app.route("/dashboard.css", methods=["GET"])
def dashboard_asset():
    """Only these bundled assets are public; query parameters cannot select files."""
    filename, mimetype = _DASHBOARD_ASSETS[request.path]
    asset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    response = send_file(asset_path, mimetype=mimetype, conditional=True, etag=True)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/api/health", methods=["GET"])
def api_health():
    """Process liveness deliberately does not create database or Redis connections."""
    return jsonify({"ok": True, "status": "alive"})


def _require_mysql_io_timeouts():
    # Connector/Python added read/write timeouts in 9.2. Refuse unbounded
    # connections on older/unknown drivers.
    version = getattr(mysql.connector, "__version__", "0.0.0")
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (ValueError, TypeError, AttributeError):
        major, minor = 0, 0
    if (major, minor) < (9, 2):
        raise ReadinessDriverUnsupported()


def _operational_mysql_options():
    _require_mysql_io_timeouts()
    return dict(
        TIDB_CONFIG,
        connection_timeout=DB_CONNECT_TIMEOUT_SEC,
        read_timeout=DB_IO_TIMEOUT_SEC,
        write_timeout=DB_IO_TIMEOUT_SEC,
    )


def _readiness_mysql_options():
    _require_mysql_io_timeouts()
    return dict(
        TIDB_CONFIG,
        connection_timeout=READINESS_TIMEOUT_SEC,
        read_timeout=READINESS_TIMEOUT_SEC,
        write_timeout=READINESS_TIMEOUT_SEC,
    )


def _close_probe_resource(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass


def check_local_database_ready():
    """Shared launcher/API probe; failures propagate for safe caller reporting."""
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**_readiness_mysql_options())
        cursor = connection.cursor(buffered=True)
        cursor.execute(LOCAL_SCHEMA_CHECK_SQL)
        return True
    finally:
        _close_probe_resource(cursor)
        _close_probe_resource(connection)


def _redis_ready():
    client = None
    try:
        import redis as redis_lib
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        client = redis_lib.Redis(
            **REDIS_CONFIG,
            socket_connect_timeout=READINESS_TIMEOUT_SEC,
            socket_timeout=READINESS_TIMEOUT_SEC,
            retry=Retry(NoBackoff(), 0),
        )
        return bool(client.ping())
    except Exception:
        return False
    finally:
        _close_probe_resource(client)


def _readiness_state(probe):
    try:
        if probe():
            return {"ok": True, "status": "ready"}
    except ReadinessDriverUnsupported as exc:
        return {"ok": False, "status": "unavailable", "reason": exc.code}
    except Exception:
        pass
    return {"ok": False, "status": "unavailable"}


@app.route("/api/ready", methods=["GET"])
def api_ready():
    """Read-only dependency probes use temporary clients, never the business pools."""
    states = {
        "database": _readiness_state(check_local_database_ready),
        "redis": _readiness_state(_redis_ready),
    }
    ready = all(component["ok"] for component in states.values())
    response = jsonify({
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "components": states,
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if ready else 503


def _internal_error(**fields):
    response = jsonify({"error": PUBLIC_INTERNAL_ERROR, **fields})
    response.headers["Cache-Control"] = "no-store"
    return response, 500


def _control_response(status, result):
    """Keep status detail while making a refused action visible to clients."""
    status["result"] = result
    succeeded = isinstance(result, dict) and result.get("ok") is True
    status["ok"] = succeeded
    if not succeeded:
        reason = result.get("error") if isinstance(result, dict) else None
        status["error"] = reason if isinstance(reason, str) and reason.strip() else "操作未完成"
    response = jsonify(status)
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if succeeded else 409


class ProxyTestResponseTooLarge(Exception):
    pass


def _read_limited_proxy_response(response):
    content = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        if len(content) + len(chunk) > PROXY_TEST_MAX_RESPONSE_BYTES:
            raise ProxyTestResponseTooLarge()
        content.extend(chunk)
    return bytes(content)


@app.errorhandler(Exception)
def _unhandled_error(exc):
    if isinstance(exc, HTTPException) and exc.code is not None and exc.code < 500:
        return exc
    return _internal_error()


def _local_authority(authority, scheme):
    if not authority or any(ord(char) <= 32 or ord(char) == 127 for char in authority):
        raise ValueError("invalid local authority")
    parsed = urlsplit(f"{scheme}://{authority}")
    if (
        parsed.netloc != authority or parsed.path or parsed.query or parsed.fragment
        or parsed.username is not None or parsed.password is not None
        or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("invalid local authority")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid local authority")
    return parsed.hostname, port or (443 if scheme == "https" else 80)


@app.before_request
def _require_local_browser_origin():
    try:
        authority = _local_authority(request.headers.get("Host", ""), request.scheme)
        origin = request.headers.get("Origin")
        if origin is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
            parsed = urlsplit(origin)
            if (
                parsed.scheme != request.scheme or parsed.path or parsed.query or parsed.fragment
                or _local_authority(parsed.netloc, parsed.scheme) != authority
            ):
                raise ValueError("invalid origin")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "仅允许从当前本机页面访问"}), 403
    return None


@app.after_request
def _disable_api_caching(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/stats")
def api_stats():
    """概览统计"""
    try:
        try_set_tiflash_read()
        return jsonify(_load_stats())
    except Exception:
        return _internal_error()


@app.route("/api/persons")
def api_persons():
    """人物列表（分页 + 搜索）。无 cursor 时仍用 page/size OFFSET。"""
    global _has_person_counts

    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.args.get("size", 20))
    except (TypeError, ValueError):
        size = 20
    page = max(page, 1)
    size = min(max(size, 1), 200)

    search = request.args.get("search", "")
    city = request.args.get("city", "")
    raw_cursor = request.args.get("cursor", "") or ""

    where = []
    params = []
    if search:
        where.append("p.full_name LIKE %s")
        params.append("%{}%".format(search))
    if city:
        where.append("p.current_city = %s")
        params.append(city)

    used_cursor = False
    cursor_name = cursor_id = None
    if raw_cursor:
        try:
            cursor_name, cursor_id = decode_cursor(raw_cursor)
        except Exception:
            return jsonify({"error": "invalid cursor"}), 400
        where.append("(p.full_name, p.person_id) > (%s, %s)")
        params.extend([cursor_name, cursor_id])
        used_cursor = True

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    count_where = [c for c in where if not c.startswith("(p.full_name, p.person_id)")]
    count_sql_where = "WHERE " + " AND ".join(count_where) if count_where else ""
    count_params = params[:-2] if used_cursor else list(params)

    def _run(use_counts):
        sql = """
            SELECT {cols}
            FROM persons p
            {where_sql}
            ORDER BY p.full_name, p.person_id
            LIMIT %s
        """.format(cols=_persons_select_cols(use_counts), where_sql=where_sql)
        qparams = list(params)
        if used_cursor:
            qparams.append(size + 1)
        else:
            offset = (page - 1) * size
            sql += " OFFSET %s"
            qparams.extend([size + 1, offset])
        return query(sql, qparams)

    try:
        rows = _run(_has_person_counts)
    except Exception as exc:
        if _has_person_counts and _is_unknown_column(exc):
            _has_person_counts = False
            rows = _run(False)
        else:
            return _internal_error()

    next_cursor = None
    if len(rows) > size:
        last = rows[size - 1]
        next_cursor = encode_cursor(last.get("full_name"), last.get("person_id"))
        rows = rows[:size]

    count_row = query_one(
        "SELECT COUNT(*) as cnt FROM persons p {}".format(count_sql_where),
        count_params,
    )
    total = _as_int((count_row or {}).get("cnt"))

    return jsonify({
        "data": rows,
        "total": total,
        "page": page,
        "size": size,
        "next_cursor": next_cursor,
    })


@app.route("/api/person/<person_id>")
def api_person_detail(person_id):
    """人物详情：全部信息"""
    try:
        person = query_one("SELECT * FROM persons WHERE person_id = %s", (person_id,))

        aliases = query(
            "SELECT alias_name FROM aliases WHERE person_id = %s", (person_id,))

        addr = query_one(
            "SELECT * FROM current_addresses WHERE person_id = %s", (person_id,))

        prev_addr = query(
            "SELECT * FROM previous_addresses WHERE person_id = %s", (person_id,))

        phones = query(
            "SELECT * FROM phone_numbers WHERE person_id = %s ORDER BY is_primary DESC",
            (person_id,))

        emails = query(
            "SELECT email FROM email_addresses WHERE person_id = %s", (person_id,))

        return jsonify({
            "person": person,
            "aliases": aliases,
            "current_address": addr,
            "previous_addresses": prev_addr,
            "phone_numbers": phones,
            "emails": emails,
        })
    except Exception:
        return _internal_error()


@app.route("/api/cities")
def api_cities():
    """Top 城市分布"""
    try:
        try_set_tiflash_read()
        rows = query("""
            SELECT current_city as city, current_state as state,
                   COUNT(*) as cnt
            FROM persons
            WHERE current_city IS NOT NULL
            GROUP BY current_city, current_state
            ORDER BY cnt DESC
            LIMIT 20
        """)
        return jsonify(rows)
    except Exception:
        return _internal_error()


@app.route("/api/age-distribution")
def api_age_dist():
    """年龄分布"""
    try:
        try_set_tiflash_read()
        rows = query("""
            SELECT
                CASE
                    WHEN age < 20 THEN '0-19'
                    WHEN age < 30 THEN '20-29'
                    WHEN age < 40 THEN '30-39'
                    WHEN age < 50 THEN '40-49'
                    WHEN age < 60 THEN '50-59'
                    WHEN age < 70 THEN '60-69'
                    ELSE '70+'
                END as age_group,
                COUNT(*) as cnt
            FROM persons
            WHERE age IS NOT NULL
            GROUP BY age_group
            ORDER BY age_group
        """)
        return jsonify(rows)
    except Exception:
        return _internal_error()


@app.route("/api/search")
def api_search():
    """全局搜索：姓名/电话/邮箱"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"persons": [], "phones": [], "emails": []})

    try:
        persons = query("""
            SELECT person_id, full_name, age, current_city, current_state
            FROM persons
            WHERE full_name LIKE %s
            LIMIT 20
        """, ("%{}%".format(q),))

        if q.isdigit():
            phones = query("""
                SELECT p.person_id, p.full_name, ph.phone_number, ph.carrier
                FROM phone_numbers ph
                JOIN persons p ON ph.person_id = p.person_id
                WHERE ph.phone_number = %s OR ph.phone_number LIKE %s
                LIMIT 20
            """, (q, q + "%"))
        else:
            phones = query("""
                SELECT p.person_id, p.full_name, ph.phone_number, ph.carrier
                FROM phone_numbers ph
                JOIN persons p ON ph.person_id = p.person_id
                WHERE ph.phone_number LIKE %s
                LIMIT 20
            """, ("%{}%".format(q),))

        emails = query("""
            SELECT p.person_id, p.full_name, e.email
            FROM email_addresses e
            JOIN persons p ON e.person_id = p.person_id
            WHERE e.email LIKE %s
            LIMIT 20
        """, ("%{}%".format(q),))

        return jsonify({"persons": persons, "phones": phones, "emails": emails})
    except Exception:
        return _internal_error()


@app.route("/api/recent")
def api_recent():
    """最近抓取的人物"""
    try:
        limit = min(max(_as_int(request.args.get("limit"), 15), 1), 100)
        try:
            rows = query("""
                SELECT person_id, full_name, age, current_city, current_state, phone_count, email_count, prev_addr_count, scraped_at
                FROM persons
                ORDER BY scraped_at DESC
                LIMIT %s
            """, (limit,))
        except Exception:
            rows = query("""
                SELECT person_id, full_name, age, current_city, current_state, scraped_at
                FROM persons
                ORDER BY scraped_at DESC
                LIMIT %s
            """, (limit,))
        return jsonify(rows)
    except Exception:
        return _internal_error()


@app.route("/api/metrics")
def api_metrics():
    """Worker 计数快照；Redis 不可用时仍 200 + 空计数。"""
    r = get_redis()
    if r is None:
        return jsonify(_empty_metrics())

    try:
        from tps_metrics import get_metrics
        payload = get_metrics(r).snapshot()
    except Exception:
        payload = _empty_metrics()

    try:
        from tps_queue import queue_stats
        payload["queue"] = queue_stats(r)
    except Exception:
        pass

    try:
        from tps_coverage import coverage_snapshot, scale_snapshot
        payload["coverage"] = coverage_snapshot(r)
        payload["scale"] = scale_snapshot(r, persons=0, use_pages=False)
    except Exception:
        pass

    try:
        from tps_control import find_role_pids
        payload["worker_running"] = bool(find_role_pids("worker"))
        payload["discover_running"] = bool(find_role_pids("discover"))
    except Exception:
        payload["worker_running"] = False
        payload["discover_running"] = False

    return jsonify(payload)


def _pipeline_payload():
    r = get_redis()
    if r is None:
        return {
            "redis_ok": False,
            "error": "Redis 不可用",
            "ts": int(time.time()),
            "queue": {},
            "discover_pending": 0,
            "discover_seen": 0,
            "worker": {"running": False, "pids": []},
            "discover": {"running": False, "pids": []},
            "jobs": [],
            "dlq_jobs": [],
            "metrics": _empty_metrics(),
            "logs": {"worker": [], "discover": []},
            "persons": 0,
            "recent": [],
            "coverage": {},
            "discover_dirs": {},
            "scale": {},
            "plan": {},
        }

    from tps_control import pipeline_status
    payload = pipeline_status(r)
    try:
        row = query_one("SELECT COUNT(*) AS n FROM persons")
        payload["persons"] = _as_int((row or {}).get("n"))
    except Exception:
        payload["persons"] = 0
    try:
        try:
            payload["recent"] = query("""
                SELECT person_id, full_name, age, current_city, current_state, phone_count, email_count, prev_addr_count, scraped_at
                FROM persons
                ORDER BY scraped_at DESC
                LIMIT 8
            """)
        except Exception:
            payload["recent"] = query("""
                SELECT person_id, full_name, age, current_city, current_state, scraped_at
                FROM persons
                ORDER BY scraped_at DESC
                LIMIT 8
            """)
    except Exception:
        payload["recent"] = []
    try:
        from tps_coverage import scale_snapshot
        payload["scale"] = scale_snapshot(r, persons=payload.get("persons") or 0, use_pages=False)
    except Exception:
        payload["scale"] = {}
    try:
        from tps_plan import build_plan, load_plan
        cfg = load_plan(r)
        scope = _as_int((payload.get("scale") or {}).get("directory_slice"))
        payload["plan"] = build_plan(
            cfg["lanes"], cfg["inflight"], cfg["page_sec"], scope, payload.get("persons") or 0
        )
    except Exception:
        payload["plan"] = {}
    try:
        from proxy_pool import load_proxy_config, mask_proxy_config
        payload["proxy_config"] = mask_proxy_config(load_proxy_config(r))
    except Exception:
        payload["proxy_config"] = {}
    return payload


@app.route("/api/pipeline")
def api_pipeline():
    """抓取开关状态、队列进度、当前任务。"""
    try:
        return jsonify(_pipeline_payload())
    except Exception:
        return _internal_error(redis_ok=False)


@app.route("/api/pipeline/scale")
def api_pipeline_scale():
    """按当前筛选预览 2.5 亿尺度，不改队列。"""
    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503
    from tps_coverage import normalize_slice, scale_snapshot
    cfg = normalize_slice(
        request.args.get("letters") or "a",
        request.args.get("states") or "",
        request.args.get("cities") or "",
        request.args.get("age_min"),
        request.args.get("age_max"),
    )
    persons = 0
    try:
        row = query_one("SELECT COUNT(*) AS n FROM persons")
        persons = _as_int((row or {}).get("n"))
    except Exception:
        persons = 0
    return jsonify(scale_snapshot(r, cfg, persons, use_pages=False))


@app.route("/api/pipeline/slice", methods=["POST"])
def api_pipeline_slice():
    """只保存切片，不开发现。"""
    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503
    body = request.get_json(silent=True) or {}
    from tps_coverage import normalize_slice, persist_slice, scale_snapshot
    cfg = normalize_slice(
        body.get("letters") or "a",
        body.get("states") or "",
        body.get("cities") or "",
        body.get("age_min"),
        body.get("age_max"),
    )
    persist_slice(r, cfg)
    persons = 0
    try:
        row = query_one("SELECT COUNT(*) AS n FROM persons")
        persons = _as_int((row or {}).get("n"))
    except Exception:
        persons = 0
    payload = scale_snapshot(r, cfg, persons, use_pages=False)
    payload["ok"] = True
    payload["slice"] = cfg
    return jsonify(payload)


@app.route("/api/pipeline/plan", methods=["GET", "POST"])
def api_pipeline_plan():
    """2.5 亿任务框架：路数、每路页数、本机实际上限。不改抓取进程。"""
    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503
    from tps_coverage import load_slice, scale_snapshot
    from tps_plan import build_plan, load_plan, persist_plan

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        cfg = persist_plan(r, {
            "lanes": body.get("lanes"),
            "inflight": body.get("inflight"),
            "page_sec": body.get("page_sec"),
        })
    else:
        cfg = load_plan(r)
    persons = 0
    try:
        row = query_one("SELECT COUNT(*) AS n FROM persons")
        persons = _as_int((row or {}).get("n"))
    except Exception:
        persons = 0
    scale = scale_snapshot(r, load_slice(r), persons, use_pages=False)
    scope = _as_int(scale.get("directory_slice"))
    plan = build_plan(cfg["lanes"], cfg["inflight"], cfg["page_sec"], scope, persons)
    plan["ok"] = True
    plan["slice_letters"] = (scale.get("slice") or {}).get("letters") or ""
    return jsonify(plan)


@app.route("/api/pipeline/dirs")
def api_pipeline_dirs():
    """目录任务明细：类型 / 字母 / 姓氏 / 状态筛选。"""
    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503
    from tps_coverage import list_discover_tasks
    payload = list_discover_tasks(
        r,
        kind=str(request.args.get("kind") or "").strip(),
        letter=str(request.args.get("letter") or "").strip(),
        q=str(request.args.get("q") or "").strip(),
        status=str(request.args.get("status") or "").strip(),
        page=_as_int(request.args.get("page"), 1),
        size=_as_int(request.args.get("size"), 30),
    )
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/pipeline/dirs", methods=["POST"])
def api_pipeline_dirs_apply():
    """action=filter 按筛选重建待扫；action=reset 恢复当前切片全部待扫。"""
    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "filter").strip().lower()
    from tps_coverage import apply_discover_filter, list_discover_tasks, load_slice, rebuild_discover_queue
    try:
        if action == "reset":
            cfg = load_slice(r)
            rebuilt = rebuild_discover_queue(r, cfg.get("letters") or "a")
            result = {"ok": True, "action": "reset", "kept": rebuilt}
        elif action == "filter":
            result = apply_discover_filter(
                r,
                kind=str(body.get("kind") or "").strip(),
                letter=str(body.get("letter") or "").strip(),
                q=str(body.get("q") or "").strip(),
            )
            result["ok"] = True
            result["action"] = "filter"
        else:
            return jsonify({"error": "action 只能是 filter 或 reset", "ok": False}), 400
    except Exception:
        return _internal_error(ok=False)
    listing = list_discover_tasks(
        r,
        kind=str(body.get("kind") or "").strip(),
        letter=str(body.get("letter") or "").strip(),
        q=str(body.get("q") or "").strip(),
        status=str(body.get("status") or "").strip(),
        page=_as_int(body.get("page"), 1),
        size=_as_int(body.get("size"), 30),
    )
    listing.update(result)
    return jsonify(listing)


@app.route("/api/pipeline/<role>", methods=["POST"])
def api_pipeline_toggle(role):
    """role=worker|discover，body: {action: start|stop, ...}"""
    if role not in ("worker", "discover"):
        return jsonify({"error": "unknown role"}), 404

    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        return jsonify({"error": "action 只能是 start 或 stop", "ok": False}), 400
    if action == "start":
        import tps_version
        force_active, force_reason = tps_version.is_force_update_active()
        if force_active:
            return jsonify({
                "ok": False,
                "code": "force_update_required",
                "error": f"系统存在关键更新必须升级：{force_reason}，请先在面板完成一键升级"
            }), 426
        refused = _start_target_guard(role)
        if refused is not None:
            return refused

    r = get_redis()
    if r is None:
        return jsonify({"error": "Redis 不可用", "ok": False}), 503

    from tps_control import start_discover, start_worker, stop_discover, stop_worker

    try:
        if role == "worker":
            result = start_worker(r, body.get("concurrency", 2)) if action == "start" else stop_worker(r)
        else:
            result = (
                start_discover(
                    r,
                    letters=body.get("letters") or "a",
                    max_dir=body.get("max_dir", 0),
                    max_persons=body.get("max_persons", 0),
                    delay=body.get("delay", 4),
                    states=body.get("states") or "",
                    cities=body.get("cities") or "",
                    age_min=body.get("age_min"),
                    age_max=body.get("age_max"),
                    reset_queue=bool(body.get("reset_queue", True)),
                )
                if action == "start"
                else stop_discover(r)
            )
    except ValueError:
        return jsonify({"error": "参数无效", "ok": False}), 400
    except Exception:
        return _internal_error(ok=False)

    status = _pipeline_payload()
    return _control_response(status, result)


# ============================================================
# IP 代理配置与连通性测试 API
# ============================================================

@app.route("/api/proxy/config", methods=["GET", "POST"])
def api_proxy_config():
    """获取或保存代理池配置（密码脱敏）"""
    r = get_redis()
    from proxy_pool import load_proxy_config, save_proxy_config, mask_proxy_config

    if request.method == "GET":
        cfg = load_proxy_config(r)
        return jsonify({"ok": True, "config": mask_proxy_config(cfg)})
    if r is None:
        return jsonify({"ok": False, "error": "Redis 不可用，未保存代理配置"}), 503

    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "direct").strip().lower()
    if mode not in ("tunnel", "file", "api", "direct"):
        return jsonify({"ok": False, "error": "无效代理模式"}), 400

    existing_cfg = load_proxy_config(r)

    tunnel = str(body.get("tunnel") or "").strip()
    host = str(body.get("host") or "").strip()
    port = str(body.get("port") or "").strip()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "").strip()

    # 如果传入了分离的 host 和 port，则组装 tunnel
    if host and port:
        # 如果密码为 ******，且存在旧密码，则复用旧密码
        if password == "******" or not password:
            old_tunnel = existing_cfg.get("tunnel") or ""
            from urllib.parse import urlparse
            try:
                old_p = urlparse(old_tunnel)
                password = old_p.password or ""
            except Exception:
                pass
        auth = f"{username}:{password}@" if username and password else (f"{username}@" if username else "")
        tunnel = f"http://{auth}{host}:{port}"

    # 如果只传了 tunnel 且用户未改密码（前端保持了脱敏串 :****@），保留原有真实密码
    if tunnel and ":****@" in tunnel:
        tunnel = existing_cfg.get("tunnel") or tunnel

    new_cfg = {
        "mode": mode,
        "tunnel": tunnel,
        "proxy_file": str(body.get("proxy_file") or "").strip(),
        "api_url": (
            existing_cfg.get("api_url") or ""
            if str(body.get("api_url") or "").strip() == "********"
            else str(body.get("api_url") or "").strip()
        ),
        "sticky_requests": _as_int(body.get("sticky_requests"), 20),
        "cooldown_sec": float(body.get("cooldown_sec") or 60.0),
    }

    try:
        saved = save_proxy_config(r, new_cfg)
    except Exception:
        return _internal_error(ok=False)
    return jsonify({
        "ok": True,
        "message": "代理配置已保存并同步至 Redis 与本地文件",
        "config": mask_proxy_config(saved),
    })


@app.route("/api/proxy/test", methods=["POST"])
def api_proxy_test():
    """在线测试指定的代理连接，使用 TLS 指纹模拟发起真实 TruePeopleSearch 探测"""
    body = request.get_json(silent=True) or {}
    proxy_url = str(body.get("proxy") or "").strip()
    test_url = str(body.get("url") or PROXY_TEST_URL).strip()
    if test_url != PROXY_TEST_URL:
        return jsonify({"ok": False, "success": False, "error": "测试目标不受支持"}), 400
    try:
        timeout = int(body.get("timeout", 12))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "success": False, "error": "测试超时参数无效"}), 400
    if not PROXY_TEST_MIN_TIMEOUT_SEC <= timeout <= PROXY_TEST_MAX_TIMEOUT_SEC:
        return jsonify({"ok": False, "success": False, "error": "测试超时参数无效"}), 400

    r = get_redis()
    from proxy_pool import load_proxy_config

    # 若未指定 proxy_url，则从已保存的配置中读取
    if not proxy_url or proxy_url == "saved":
        cfg = load_proxy_config(r)
        if cfg.get("mode") == "tunnel" and cfg.get("tunnel"):
            proxy_url = cfg.get("tunnel")
        elif cfg.get("mode") == "direct":
            proxy_url = ""
    elif proxy_url == "direct":
        proxy_url = ""

    # 若前端传来的带掩码 :****@，则尝试用已保存的真实密码恢复
    if ":****@" in proxy_url:
        saved_cfg = load_proxy_config(r)
        if saved_cfg.get("tunnel"):
            proxy_url = saved_cfg.get("tunnel")

    # 发起请求测试
    from protocol_fetcher import DEFAULT_HEADERS, check_cloudflare_blocked
    try:
        from curl_cffi.requests import Session
        import time as _t

        start_t = _t.perf_counter()
        with Session(impersonate="chrome124") as s:
            kwargs = {
                "headers": DEFAULT_HEADERS,
                "timeout": timeout,
                "allow_redirects": False,
                "stream": True,
            }
            if proxy_url:
                kwargs["proxy"] = proxy_url

            resp = s.get(test_url, **kwargs)
            try:
                content = _read_limited_proxy_response(resp)
            finally:
                resp.close()
            elapsed_ms = round((_t.perf_counter() - start_t) * 1000, 1)

            status = resp.status_code
            html = content.decode("utf-8", errors="replace")
            cf_blocked = check_cloudflare_blocked(status, html)

            if cf_blocked:
                return jsonify({
                    "ok": True,
                    "success": False,
                    "status_code": status,
                    "latency_ms": elapsed_ms,
                    "cf_blocked": True,
                    "bytes": len(content),
                    "message": "Cloudflare 5秒盾拦截（该代理 IP 指纹被标记）",
                })
            elif status == 200:
                return jsonify({
                    "ok": True,
                    "success": True,
                    "status_code": status,
                    "latency_ms": elapsed_ms,
                    "cf_blocked": False,
                    "bytes": len(content),
                    "message": "连接成功！TLS 指纹通过，Cloudflare 顺利穿透",
                })
            else:
                return jsonify({
                    "ok": True,
                    "success": False,
                    "status_code": status,
                    "latency_ms": elapsed_ms,
                    "cf_blocked": False,
                    "bytes": len(content),
                    "message": f"返回异常 HTTP {status}",
                })

    except ProxyTestResponseTooLarge:
        return jsonify({"ok": False, "success": False, "error": "测试响应超过大小限制"}), 413
    except Exception:
        return jsonify({
            "ok": False,
            "success": False,
            "error": PUBLIC_INTERNAL_ERROR,
            "message": "代理握手失败或连接超时",
        }), 200


# ============================================================
# 3000万级高通量集群控制 API
# ============================================================

@app.route("/api/cluster/status", methods=["GET"])
def api_cluster_status():
    """获取高通量多进程集群状态"""
    r = get_redis()
    if r is None:
        return jsonify({"ok": False, "error": "Redis 不可用"}), 503
    from tps_control import cluster_status
    from proxy_pool import load_proxy_config, mask_proxy_config

    status = cluster_status(r)
    status["proxy_config"] = mask_proxy_config(load_proxy_config(r))
    status["ok"] = True
    return jsonify(status)


@app.route("/api/cluster/control", methods=["POST"])
def api_cluster_control():
    """启动或停止 3000万级高通量抓取集群"""
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "action 必须是 start 或 stop"}), 400
    if action == "start":
        import tps_version
        force_active, force_reason = tps_version.is_force_update_active()
        if force_active:
            return jsonify({
                "ok": False,
                "code": "force_update_required",
                "error": f"系统存在关键更新必须升级：{force_reason}，请先在面板完成一键升级"
            }), 426
        refused = _start_target_guard("cluster")
        if refused is not None:
            return refused

    r = get_redis()
    if r is None:
        return jsonify({"ok": False, "error": "Redis 不可用"}), 503

    from tps_control import start_cluster, stop_cluster, cluster_status

    try:
        if action == "start":
            workers = _as_int(body.get("workers"), 4)
            concurrency = _as_int(body.get("concurrency"), 80)
            decoupled = bool(body.get("decoupled", True))
            proxy_tunnel = body.get("proxy_tunnel") or None
            res = start_cluster(
                r,
                workers=workers,
                concurrency=concurrency,
                decoupled=decoupled,
                proxy_tunnel=proxy_tunnel,
            )
        else:
            res = stop_cluster(r)
    except Exception:
        return _internal_error(ok=False)

    status = cluster_status(r)
    return _control_response(status, res)


# ============================================================
# 系统版本与在线更新 API
# ============================================================

@app.route("/api/system/version", methods=["GET"])
def system_version():
    """获取当前系统运行版本、Git 状态与更新配置"""
    try:
        import tps_version
        local_info = tps_version.read_local_version_info()
        git_info = tps_version.get_git_status()
        return jsonify({
            "ok": True,
            "version": local_info.get("version", "1.0.0"),
            "build": local_info.get("build", ""),
            "release_date": local_info.get("release_date", ""),
            "channel": local_info.get("channel", "stable"),
            "name": local_info.get("name", "TruePeopleSearch Enterprise Intelligence"),
            "release_notes": local_info.get("release_notes", []),
            "git": git_info,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/system/check-update", methods=["GET", "POST"])
def system_check_update():
    """检查远端新版本 (Git 远程与 Manifest 双通道)"""
    try:
        import tps_version
        update_info = tps_version.check_for_updates(timeout_sec=6.0)
        return jsonify(update_info)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/system/apply-update", methods=["POST"])
def system_apply_update():
    """触发安全一键在线更新与平滑重载"""
    try:
        import tps_version
        body = request.get_json(silent=True) or {}
        force_stash = bool(body.get("force_stash", False))
        res = tps_version.execute_system_update(force_stash=force_stash)
        status_code = 200 if res.get("ok") else 400
        return jsonify(res), status_code
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ============================================================
# 入口
# ============================================================

def find_available_port(host: str, start_port: int, max_attempts: int = 20) -> int:
    import socket
    host = _loopback_host(host, "--host")
    if not 1 <= start_port <= 65535 or max_attempts < 1:
        raise ValueError("Port must be between 1 and 65535, and max_attempts must be positive")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for p in range(start_port, min(start_port + max_attempts, 65536)):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError("No available dashboard port in the requested range; choose another --port")


def _port_argument(value):
    try:
        return _config_port({"--port": value}, "--port", 5001)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _host_argument(value):
    try:
        return _loopback_host(value, "--host")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=_port_argument, default=DASHBOARD_CONFIG["port"], help="启动端口 (默认 5001，可通过 TPS_DASHBOARD_PORT 配置)")
    parser.add_argument("--host", type=_host_argument, default=DASHBOARD_CONFIG["host"], help="监听地址 (仅 127.0.0.1、::1、localhost)")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    try:
        actual_port = find_available_port(args.host, args.port)
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"[错误] {exc}\n")
    if actual_port != args.port:
        print(f"\n[提示] 端口 {args.port} 已被占用 (macOS AirPlay 接收器默认占用 5000)，已自动切换至空闲端口: {actual_port}")

    browser_host = args.host
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    print(f"\n{'='*50}")
    print(f"  TruePeopleSearch 可视化管理面板")
    print(f"  浏览器访问: http://{browser_host}:{actual_port}")
    print(f"  代理配置页: http://{browser_host}:{actual_port}/#/proxy")
    print(f"{'='*50}\n")

    app.run(host=args.host, port=actual_port, debug=False)
