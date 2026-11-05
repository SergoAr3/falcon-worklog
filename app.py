"""Time Tracker: dependency-free HTTP API, SQLite persistence, and static UI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def utc_iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def date_boundary(value: str) -> int:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ApiError(400, "Dates must use YYYY-MM-DD format.")
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "Invalid calendar date.") from exc
    return int(datetime.combine(day, datetime_time.min, tzinfo=timezone.utc).timestamp())


class TimeTrackerStore:
    """A connection per call; transactions prevent double-active timers."""

    def __init__(self, path: str | Path, clock=None):
        self.path = str(path)
        self.clock = clock or (lambda: int(time.time()))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL REFERENCES employees(id),
                    description TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER,
                    CHECK (ended_at IS NULL OR ended_at >= started_at)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_entry_per_employee
                    ON entries(employee_id) WHERE ended_at IS NULL;
                CREATE INDEX IF NOT EXISTS entries_for_report
                    ON entries(employee_id, started_at);
            """)

    @staticmethod
    def _employee_id(value):
        if type(value) is not int or value <= 0:
            raise ApiError(400, "employee_id must be a positive integer.")
        return value

    def add_employee(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ApiError(400, "Name must be between 1 and 80 characters.")
        name = name.strip()
        with self._connect() as db:
            cur = db.execute("INSERT INTO employees(name) VALUES (?)", (name,))
            return {"id": cur.lastrowid, "name": name, "active_entry": None}

    def employees(self):
        with self._connect() as db:
            rows = db.execute("""
                SELECT e.id, e.name, s.id AS entry_id, s.description, s.started_at
                FROM employees e LEFT JOIN entries s
                    ON s.employee_id = e.id AND s.ended_at IS NULL
                ORDER BY e.id ASC
            """).fetchall()
        return [{
            "id": row["id"], "name": row["name"],
            "active_entry": {
                "id": row["entry_id"],
                "description": row["description"],
                "started_at": utc_iso(row["started_at"]),
            } if row["entry_id"] is not None else None,
        } for row in rows]

    def start(self, employee_id, description):
        employee_id = self._employee_id(employee_id)
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 240:
            raise ApiError(400, "Task description must be between 1 and 240 characters.")
        description = description.strip()
        now = int(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM employees WHERE id=?", (employee_id,)).fetchone():
                raise ApiError(404, "Employee not found.")
            try:
                cur = db.execute(
                    "INSERT INTO entries(employee_id, description, started_at) VALUES (?, ?, ?)",
                    (employee_id, description, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "This employee already has a running timer.") from exc
        return {"id": cur.lastrowid, "employee_id": employee_id,
                "description": description, "started_at": utc_iso(now), "ended_at": None}

    def stop(self, employee_id):
        employee_id = self._employee_id(employee_id)
        now = int(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM employees WHERE id=?", (employee_id,)).fetchone():
                raise ApiError(404, "Employee not found.")
            row = db.execute("""
                SELECT id, description, started_at FROM entries
                WHERE employee_id=? AND ended_at IS NULL
            """, (employee_id,)).fetchone()
            if row is None:
                raise ApiError(409, "No running timer for this employee.")
            # Keep the database invariant if the machine clock moves backwards.
            ended_at = max(now, row["started_at"])
            db.execute("UPDATE entries SET ended_at=? WHERE id=?", (ended_at, row["id"]))
        return {"id": row["id"], "employee_id": employee_id,
                "description": row["description"],
                "started_at": utc_iso(row["started_at"]), "ended_at": utc_iso(ended_at),
                "duration_seconds": ended_at - row["started_at"]}

    def report(self, from_date, to_date):
        start = date_boundary(from_date)
        end_day = date_boundary(to_date)
        if end_day < start:
            raise ApiError(400, "'to' must be on or after 'from'.")
        # End is exclusive: the full selected final UTC day is included.
        end = end_day + 86400
        now = int(self.clock())
        with self._connect() as db:
            db.execute("BEGIN")
            people = db.execute("SELECT id, name FROM employees ORDER BY id").fetchall()
            rows = db.execute("""
                SELECT id, employee_id, description, started_at, ended_at
                FROM entries
                WHERE started_at < ? AND COALESCE(ended_at, ?) >= ?
                ORDER BY started_at DESC, id DESC
            """, (end, now, start)).fetchall()

        by_employee = {p["id"]: {"id": p["id"], "name": p["name"],
                                   "total_seconds": 0, "records": []} for p in people}
        for row in rows:
            real_end = now if row["ended_at"] is None else row["ended_at"]
            overlap = max(0, min(real_end, end) - max(row["started_at"], start))
            # Zero-second sessions still belong to the report for their start date.
            started_in_period = start <= row["started_at"] < end
            if overlap <= 0 and not started_in_period:
                continue
            record = {
                "id": row["id"], "description": row["description"],
                "started_at": utc_iso(row["started_at"]),
                "ended_at": utc_iso(row["ended_at"]) if row["ended_at"] is not None else None,
                "duration_seconds": overlap,
                "is_running": row["ended_at"] is None,
            }
            person = by_employee[row["employee_id"]]
            person["records"].append(record)
            person["total_seconds"] += overlap
        employees = list(by_employee.values())
        return {"from": from_date, "to": to_date, "generated_at": utc_iso(now),
                "total_seconds": sum(p["total_seconds"] for p in employees),
                "employees": employees}


def make_handler(store: TimeTrackerStore, static_dir: Path = STATIC_DIR):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Worklog/1.0"

        def _json(self, data, status=200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _body(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError(400, "Invalid Content-Length.") from exc
            if not 0 < size <= 8192:
                raise ApiError(400, "JSON body is required (maximum 8 KB).")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ApiError(415, "Content-Type must be application/json.")
            try:
                body = json.loads(self.rfile.read(size))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "Malformed JSON.") from exc
            if not isinstance(body, dict):
                raise ApiError(400, "Expected a JSON object.")
            return body

        def _serve_static(self, path):
            routes = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
            filename = routes.get(path)
            if not filename:
                raise ApiError(404, "Not found.")
            data = (static_dir / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/api/employees":
                    self._json({"employees": store.employees()})
                elif parsed.path == "/api/report":
                    params = parse_qs(parsed.query)
                    today = datetime.fromtimestamp(store.clock(), tz=timezone.utc).date().isoformat()
                    self._json(store.report(params.get("from", [today])[0], params.get("to", [today])[0]))
                else:
                    self._serve_static(parsed.path)
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                if path not in ("/api/employees", "/api/timer/start", "/api/timer/stop"):
                    raise ApiError(404, "Not found.")
                body = self._body()
                if path == "/api/employees":
                    self._json(store.add_employee(body.get("name")), 201)
                elif path == "/api/timer/start":
                    self._json(store.start(body.get("employee_id"), body.get("description")), 201)
                else:
                    self._json(store.stop(body.get("employee_id")))
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)

    return Handler


def build_server(host="127.0.0.1", port=8000, db_path=None, clock=None):
    path = db_path or os.environ.get("TIME_TRACKER_DB", str(BASE_DIR / "data" / "tracker.sqlite3"))
    store = TimeTrackerStore(path, clock=clock)
    return ThreadingHTTPServer((host, port), make_handler(store))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch Worklog")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite database file")
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.db)
    print(f"Time Tracker running at http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()
