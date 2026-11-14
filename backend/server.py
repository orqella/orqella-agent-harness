"""Small sidecar for the installable OpenCode module.

OpenCode owns its own service and credentials. This sidecar reads the local
service credential and talks to the HTTP API directly, so this module needs no
OpenCode-specific host routes in Orqella.
"""
import base64
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

TOKEN = os.environ.get("ORQELLA_MODULE_TOKEN", "")
CONFIG = json.loads(os.environ.get("ORQELLA_MODULE_CONFIG") or "{}")
HOST_HOME = os.environ.get("ORQELLA_HOST_HOME") or os.path.expanduser("~")
DEFAULT_BASE_URL = "http://127.0.0.1:49374"
CONFIGURED_BASE_URL = (CONFIG.get("base_url") or "").rstrip("/")
SERVICE_START_TIMEOUT = 20
SERVICE_WAIT_SECONDS = 15
SERVICE_READY_SECONDS = 10
_service_lock = threading.Lock()
_service_ready_until = 0.0


def service_candidates():
    """OpenCode keeps the managed-service credential beside its config.

    The default is ~/.config/opencode on Linux, macOS, and Windows. Some
    OpenCode builds also use the XDG state directory or a platform config
    directory, so check those locations before reporting the service missing.
    """
    names = [
        (".config", "opencode", "service.json"),
        (".local", "state", "opencode", "service.json"),
        ("Library", "Application Support", "opencode", "service.json"),
        ("AppData", "Roaming", "opencode", "service.json"),
        ("AppData", "Local", "opencode", "service.json"),
    ]
    return [os.path.join(HOST_HOME, *parts) for parts in names]


def service_config():
    for path in service_candidates():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                return value, path
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise RuntimeError(f"could not read OpenCode service credential at {path}: {error}") from error
    raise RuntimeError(
        "OpenCode service credential not found. Run `opencode service start` "
        "(or start OpenCode once) so service.json is created, then retry."
    )


def service_base_url():
    if CONFIGURED_BASE_URL and CONFIGURED_BASE_URL != DEFAULT_BASE_URL:
        return CONFIGURED_BASE_URL
    service, _path = service_config()
    try:
        port = int(service.get("port") or 49374)
    except (TypeError, ValueError):
        port = 49374
    return f"http://127.0.0.1:{port}"


def service_health():
    """Return true when the configured service answers with its credential."""
    try:
        service, _path = service_config()
        password = service.get("password") or ""
        request = Request(
            service_base_url() + "/api/info",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"opencode:{password}".encode()).decode()
            },
        )
        with urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def opencode_binary():
    """Find the OpenCode CLI without assuming it is on the daemon PATH."""
    candidates = [
        os.environ.get("ORQELLA_OPENCODE_BIN"),
        shutil.which("opencode"),
        os.path.join(HOST_HOME, ".local", "share", "mise", "shims", "opencode"),
        os.path.join(HOST_HOME, ".local", "bin", "opencode"),
        "/usr/local/bin/opencode",
        "/usr/bin/opencode",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ensure_service():
    """Reuse a running OpenCode service, or start the shared one on demand.

    `opencode service start` is idempotent: it returns the existing service
    URL when one is already running. Starting a second `opencode serve` on the
    same port is what causes the crash loops this avoids.
    """
    global _service_ready_until
    if CONFIGURED_BASE_URL and CONFIGURED_BASE_URL != DEFAULT_BASE_URL:
        # An explicit non-default URL is authoritative. Do not start a local
        # service that the user did not ask for.
        return
    if time.monotonic() < _service_ready_until and service_health():
        return
    with _service_lock:
        if service_health():
            _service_ready_until = time.monotonic() + SERVICE_READY_SECONDS
            return
        binary = opencode_binary()
        if not binary:
            raise RuntimeError(
                "OpenCode is not installed or not on PATH. Install OpenCode, "
                "or set the module's base_url to an existing OpenCode service."
            )
        try:
            result = subprocess.run(
                [binary, "service", "start"],
                capture_output=True,
                text=True,
                timeout=SERVICE_START_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"could not start the OpenCode service: {error}") from error
        deadline = time.monotonic() + SERVICE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if service_health():
                _service_ready_until = time.monotonic() + SERVICE_READY_SECONDS
                return
            time.sleep(0.5)
        detail = (result.stderr or result.stdout or "").strip()
        message = "OpenCode service did not become reachable."
        if detail:
            message += f" {detail[-800:]}"
        else:
            message += " Try `opencode service restart`."
        raise RuntimeError(message)


def basic_auth():
    service, _path = service_config()
    password = service.get("password") or ""
    return base64.b64encode(f"opencode:{password}".encode()).decode()


def opencode_api(method, path, body=None, accept=None):
    ensure_service()
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Basic " + basic_auth(),
        "Content-Type": "application/json",
    }
    if accept:
        headers["Accept"] = accept
    request = Request(
        service_base_url() + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=60) as response:
            output = response.read().decode()
    except HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        raise RuntimeError(detail[-800:] or f"OpenCode returned HTTP {error.code}") from error
    except Exception as error:
        raise RuntimeError(f"could not reach OpenCode: {error}") from error
    return json.loads(output) if output.strip() else {}


def public_model(model):
    return {
        "id": model.get("id"),
        "modelID": model.get("modelID") or model.get("id"),
        "providerID": model.get("providerID"),
        "name": model.get("name") or model.get("id"),
        "family": model.get("family"),
        "enabled": model.get("enabled", True),
        "status": model.get("status"),
    }


def models():
    available = opencode_api("GET", "/api/model").get("data", [])
    default = opencode_api("GET", "/api/model/default").get("data")
    providers = opencode_api("GET", "/api/provider").get("data", [])
    return {
        "models": [public_model(model) for model in available if model.get("enabled", True)],
        "default": public_model(default) if isinstance(default, dict) else None,
        "providers": [
            {"id": provider.get("id"), "name": provider.get("name") or provider.get("id")}
            for provider in providers
            if provider.get("id")
        ],
    }


def sessions(query):
    try:
        limit = max(1, min(100, int(query.get("limit", ["25"])[0])))
    except (TypeError, ValueError):
        limit = 25
    parameters = {"limit": limit}
    if query.get("cursor"):
        parameters["cursor"] = query["cursor"][0]
    result = opencode_api("GET", "/api/session?" + urlencode(parameters))
    rows = []
    for session in result.get("data", []):
        rows.append({
            "id": session.get("id"),
            "title": session.get("title"),
            "parentID": session.get("parentID"),
            "agent": session.get("agent"),
            "model": session.get("model"),
            "time": session.get("time"),
        })
    return {"data": rows, "cursor": result.get("cursor")}


def pair():
    result = opencode_api("POST", "/api/pair", {})
    code = str(result.get("code") or "")
    if not code:
        raise RuntimeError("OpenCode did not return a pairing code")
    auth = opencode_api(
        "GET",
        "/auth/connect/" + quote(code, safe=""),
        accept="application/json",
    )
    token = str(auth.get("token") or "")
    if not token:
        raise RuntimeError("OpenCode did not return an authentication token")
    encoded = base64.urlsafe_b64encode(f"opencode:{token}".encode()).decode().rstrip("=")
    return {"code": code, "auth_token": encoded}


def launch(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("a prompt is required")
    model = payload.get("model") or {}
    model_id = model.get("id") or model.get("modelID")
    provider_id = model.get("providerID")
    if not model_id or not provider_id:
        raise ValueError("a model is required")
    model_value = {"id": model_id, "providerID": provider_id}
    if model.get("variant"):
        model_value["variant"] = model["variant"]
    created = opencode_api("POST", "/api/session", {"model": model_value})
    session = created.get("data") or created
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError("OpenCode did not return a session id")
    message = opencode_api(
        "POST",
        "/api/session/" + quote(session_id, safe="") + "/prompt",
        {"text": prompt},
    )
    return {"session": session, "message": message.get("data") or message}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("X-Orqella-Module-Token") == TOKEN:
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
        if length > 2 * 1024 * 1024:
            raise ValueError("request body too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def do_GET(self):
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                return self.send_json(200, {"ok": True})
            if parsed.path == "/api/service":
                ensure_service()
                return self.send_json(200, {"ok": True, "url": service_base_url()})
            if parsed.path == "/api/models":
                return self.send_json(200, models())
            if parsed.path == "/api/sessions":
                return self.send_json(200, sessions(parse_qs(parsed.query)))
            return self.send_json(404, {"error": "not found"})
        except Exception as error:
            return self.send_json(502, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/launch":
                return self.send_json(200, launch(self.read_json()))
            if parsed.path == "/api/pair":
                return self.send_json(200, pair())
            return self.send_json(404, {"error": "not found"})
        except Exception as error:
            return self.send_json(502, {"error": str(error)})


if __name__ == "__main__":
    port = int(os.environ["ORQELLA_MODULE_PORT"])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
