"""Agent Harness sidecar.

One loopback HTTP server plus a WebSocket event stream. Commands arrive over
HTTP; normalized session events leave over `/ws/sessions/<id>`.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import struct
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import native
from acp import AcpError, AcpProtocolError, AcpUnavailable
from sessions import SessionManager

TOKEN = os.environ.get("ORQELLA_MODULE_TOKEN", "")
CONFIG = json.loads(os.environ.get("ORQELLA_MODULE_CONFIG") or "{}")
HOST_HOME = os.environ.get("ORQELLA_HOST_HOME") or os.path.expanduser("~")
DATA_DIR = os.environ.get("AGENT_HARNESS_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY = 16 * 1024 * 1024

manager = SessionManager(DATA_DIR, CONFIG)


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# -- WebSocket framing ----------------------------------------------------


def ws_send(sock, payload, opcode=1):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    sock.sendall(bytes(header) + payload)


def ws_read(sock):
    """Read one client frame. Returns (opcode, payload) or (None, b"")."""
    try:
        first = sock.recv(2)
    except (OSError, ValueError):
        return None, b""
    if len(first) < 2:
        return None, b""
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > MAX_BODY:
        return 8, b""
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked and payload:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _recv_exact(sock, count):
    chunks = b""
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise OSError("socket closed")
        chunks += chunk
    return chunks


# -- HTTP handler ---------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "OrqellaAgentHarness/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        supplied = self.headers.get("X-Orqella-Module-Token") or ""
        if TOKEN and hmac.compare_digest(supplied, TOKEN):
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise RequestError(413, "request body too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RequestError(400, "invalid JSON") from error
        if not isinstance(value, dict):
            raise RequestError(400, "JSON object required")
        return value

    def route(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    def do_GET(self):
        path, query = self.route()
        if path.startswith("/ws/"):
            return self.handle_websocket(path, query)
        if not self.authorized():
            return
        try:
            if path == "/health":
                return self.send_json(200, {"ok": True})
            if path == "/api/registry":
                return self.send_json(200, manager.registry_value())
            if path == "/api/harnesses":
                return self.send_json(200, manager.harnesses())
            if path == "/api/sessions":
                harness = (query.get("harness") or [""])[0]
                limit = (query.get("limit") or ["50"])[0]
                return self.send_json(200, {"sessions": manager.list(harness or None, limit)})
            if path == "/api/sessions/discover":
                harness = (query.get("harness") or [""])[0]
                return self.send_json(200, manager.discover(harness))
            if path.startswith("/api/sessions/"):
                rest = path[len("/api/sessions/"):]
                if rest.endswith("/transcript"):
                    session_id = rest[: -len("/transcript")]
                    since = (query.get("since") or ["0"])[0]
                    return self.send_json(200, {"events": manager.transcript(session_id, since)})
                if rest.endswith("/commands"):
                    session_id = rest[: -len("/commands")]
                    return self.send_json(200, manager.commands(session_id))
                row = manager.get(rest)
                return self.send_json(200, manager.public_session(row))
            if path == "/api/native/status":
                harness = (query.get("harness") or [""])[0]
                return self.send_json(200, native.status(manager.entry(harness), HOST_HOME))
            if path == "/api/native/activity":
                return self.send_json(200, {"sessions": manager.native_activity()})
            if path == "/api/fs/dirs":
                return self.send_json(200, list_directories((query.get("path") or [HOST_HOME])[0]))
            if path.startswith("/api/terminals/"):
                return self.send_json(200, manager.terminal_output(path[len("/api/terminals/"):]))
            return self.send_json(404, {"error": "not found"})
        except RequestError as error:
            return self.send_json(error.status, {"error": str(error)})
        except KeyError as error:
            return self.send_json(404, {"error": f"not found: {error}"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except (AcpUnavailable, AcpProtocolError) as error:
            return self.send_json(502, {"error": str(error)})
        except (AcpError, RuntimeError, OSError) as error:
            return self.send_json(502, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - API boundary
            manager.log(traceback.format_exc())
            return self.send_json(500, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return
        path, _query = self.route()
        try:
            body = self.read_json()
            if path == "/api/registry":
                return self.send_json(200, manager.set_registry(body))
            if path == "/api/registry/probe":
                harness_id = str(body.get("id") or "")
                return self.send_json(200, manager.probe(harness_id))
            if path == "/api/sessions/forget":
                return self.send_json(
                    200,
                    manager.forget(
                        str(body.get("harness") or ""),
                        str(body.get("session_id") or ""),
                        native=body.get("native", True) is not False,
                    ),
                )
            if path == "/api/sessions/adopt":
                return self.send_json(
                    200,
                    manager.adopt(
                        str(body.get("harness") or ""),
                        str(body.get("session_id") or ""),
                        str(body.get("cwd") or ""),
                        str(body.get("title") or ""),
                    ),
                )
            if path == "/api/sessions":
                created = manager.create(
                    str(body.get("harness") or ""),
                    str(body.get("cwd") or ""),
                    str(body.get("title") or ""),
                    body.get("additional_directories") or None,
                )
                return self.send_json(200, created)
            if path.startswith("/api/sessions/"):
                parts = path[len("/api/sessions/"):].split("/")
                session_id = parts[0]
                action = parts[1] if len(parts) > 1 else ""
                if action == "prompt":
                    return self.send_json(
                        200,
                        manager.prompt(session_id, body.get("text"), body.get("images") or []),
                    )
                if action == "command":
                    return self.send_json(
                        200,
                        manager.command(
                            session_id,
                            str(body.get("name") or ""),
                            str(body.get("text") or ""),
                            body.get("images") or [],
                        ),
                    )
                if action == "cancel":
                    return self.send_json(200, manager.cancel(session_id))
                if action == "permission":
                    return self.send_json(
                        200,
                        manager.resolve_permission(
                            str(body.get("request_id") or ""),
                            body.get("option_id") or None,
                        ),
                    )
                if action == "config":
                    value = body.get("value")
                    return self.send_json(
                        200,
                        manager.config(
                            session_id,
                            str(body.get("config_id") or ""),
                            value,
                            boolean=body.get("type") == "boolean",
                        ),
                    )
                if action == "resume":
                    return self.send_json(200, manager.resume(session_id))
                if action == "refresh":
                    return self.send_json(200, manager.refresh(session_id))
                if action == "history":
                    return self.send_json(
                        200,
                        manager.history(session_id, str(body.get("cursor") or "")),
                    )
                if action == "roots":
                    directories = [str(item) for item in body.get("directories") or [] if str(item).strip()]
                    row = manager.get(session_id)
                    row["root_set"] = list(dict.fromkeys([row["cwd"], *map(os.path.abspath, directories)]))
                    manager._save_sessions()
                    return self.send_json(200, {"root_set": row["root_set"]})
            if path == "/api/native/start":
                harness = str(body.get("harness") or "")
                return self.send_json(200, native.start(manager.entry(harness), HOST_HOME))
            if path == "/api/native/pair":
                harness = str(body.get("harness") or "")
                return self.send_json(200, native.pair(manager.entry(harness), HOST_HOME))
            return self.send_json(404, {"error": "not found"})
        except RequestError as error:
            return self.send_json(error.status, {"error": str(error)})
        except KeyError as error:
            return self.send_json(404, {"error": f"not found: {error}"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except (AcpUnavailable, AcpProtocolError) as error:
            return self.send_json(502, {"error": str(error)})
        except (AcpError, RuntimeError, OSError) as error:
            return self.send_json(502, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - API boundary
            manager.log(traceback.format_exc())
            return self.send_json(500, {"error": str(error)})

    def do_PUT(self):
        return self.do_POST()

    def do_DELETE(self):
        if not self.authorized():
            return
        path, query = self.route()
        try:
            if path.startswith("/api/sessions/"):
                native = (query.get("native") or ["1"])[0].lower() not in {"0", "false", "no"}
                return self.send_json(200, manager.delete(path[len("/api/sessions/"):], native=native))
            return self.send_json(404, {"error": "not found"})
        except KeyError as error:
            return self.send_json(404, {"error": f"not found: {error}"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - API boundary
            manager.log(traceback.format_exc())
            return self.send_json(500, {"error": str(error)})

    # -- WebSocket --------------------------------------------------------

    def handle_websocket(self, path, query):
        if not self.authorized():
            return
        key = self.headers.get("Sec-WebSocket-Key") or ""
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or upgrade != "websocket":
            return self.send_json(400, {"error": "websocket upgrade required"})
        session_id = path[len("/ws/sessions/"):] if path.startswith("/ws/sessions/") else ""
        try:
            manager.get(session_id)
        except KeyError:
            return self.send_json(404, {"error": "session not found"})
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        try:
            self.stream_events(session_id, (query.get("since") or ["0"])[0])
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.connection.close()
            except OSError:
                pass

    def stream_events(self, session_id, since):
        subscriber = manager.subscribe(session_id)
        last = int(since or 0)
        try:
            backlog = manager.transcript(session_id, last)
            for event in backlog:
                ws_send(self.connection, json.dumps(event))
                last = max(last, int(event.get("seq") or 0))
            ws_send(
                self.connection,
                json.dumps({"type": "ready", "session": manager.public_session(manager.get(session_id))}),
            )
            # Read client frames (close/ping) on a side thread; the main loop
            # only writes events.
            stop = threading.Event()

            def drain():
                while not stop.is_set():
                    opcode, payload = ws_read(self.connection)
                    if opcode is None or opcode == 8:
                        stop.set()
                        return
                    if opcode == 9:
                        try:
                            ws_send(self.connection, payload, opcode=10)
                        except OSError:
                            stop.set()
                            return

            threading.Thread(target=drain, name=f"ws-{session_id}", daemon=True).start()
            while not stop.is_set():
                try:
                    event = subscriber.get(timeout=25)
                except queue.Empty:
                    try:
                        ws_send(self.connection, b"", opcode=9)
                    except OSError:
                        return
                    continue
                ws_send(self.connection, json.dumps(event))
                last = max(last, int(event.get("seq") or 0))
        finally:
            manager.unsubscribe(session_id, subscriber)


def list_directories(path):
    path = os.path.abspath(os.path.expanduser(str(path or HOST_HOME)))
    if not os.path.isdir(path):
        raise ValueError(f"`{path}` is not a directory")
    entries = []
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except OSError as error:
        raise ValueError(f"could not read {path}: {error}") from error
    for name in names:
        if name.startswith("."):
            continue
        candidate = os.path.join(path, name)
        try:
            if os.path.isdir(candidate):
                entries.append({"name": name, "path": candidate})
        except OSError:
            continue
        if len(entries) >= 500:
            break
    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if parent != path else None,
        "home": HOST_HOME,
        "entries": entries,
    }


def main():
    port = int(os.environ["ORQELLA_MODULE_PORT"])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        manager.close()


if __name__ == "__main__":
    main()
