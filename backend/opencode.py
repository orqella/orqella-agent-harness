"""OpenCode native workspace helper.

OpenCode owns its service and credentials. The module uses that service as the
session transport, so prompts are shared with every OpenCode client. This file
holds the service discovery, credentials, activity hint, and the pairing token
used by the explicit "Open native workspace" action.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:49374"
SERVICE_START_TIMEOUT = 20
SERVICE_WAIT_SECONDS = 15
SERVICE_READY_SECONDS = 10

_service_lock = threading.Lock()
_service_ready_until: dict[str, float] = {}


class ServiceCredentialMissing(RuntimeError):
    """The OpenCode service has not created service.json yet."""


class ServiceCredentialInvalid(RuntimeError):
    """service.json exists but cannot be read or parsed."""


def configured_base_url(config):
    return str(config.get("base_url") or "").rstrip("/")


def service_candidates(host_home):
    """OpenCode keeps the managed-service credential beside its config."""
    names = [
        (".config", "opencode", "service.json"),
        (".local", "state", "opencode", "service.json"),
    ]
    if sys.platform == "darwin":
        names.append(("Library", "Application Support", "opencode", "service.json"))
    if os.name == "nt":
        names.extend(
            [
                ("AppData", "Roaming", "opencode", "service.json"),
                ("AppData", "Local", "opencode", "service.json"),
            ]
        )
    return [os.path.join(host_home, *parts) for parts in names]


def service_config(config, host_home):
    for path in service_candidates(host_home):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                return value, path
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise ServiceCredentialInvalid(
                f"could not read OpenCode service credential at {path}: {error}"
            ) from error
    raise ServiceCredentialMissing(
        "OpenCode service credential not found. Run `opencode service start` "
        "(or start OpenCode once) so service.json is created, then retry."
    )


def service_base_url(config, host_home):
    configured = configured_base_url(config)
    if configured and configured != DEFAULT_BASE_URL:
        return configured
    service, _path = service_config(config, host_home)
    try:
        port = int(service.get("port") or 49374)
    except (TypeError, ValueError):
        port = 49374
    return f"http://127.0.0.1:{port}"


def configured_base_is_local(config):
    from urllib.parse import urlparse

    configured = configured_base_url(config)
    if not configured or configured == DEFAULT_BASE_URL:
        return True
    host = (urlparse(configured).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def service_health(config, host_home):
    """Return true when the configured service answers with its credential."""
    if not configured_base_is_local(config):
        return False
    try:
        service, _path = service_config(config, host_home)
        password = service.get("password") or ""
        request = Request(
            service_base_url(config, host_home) + "/api/info",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"opencode:{password}".encode()).decode()
            },
        )
        with urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except ServiceCredentialInvalid:
        raise
    except Exception:
        return False


def service_status(config, host_home):
    if not configured_base_is_local(config):
        return {
            "ok": False,
            "running": False,
            "url": configured_base_url(config),
            "error": "OpenCode base_url must be loopback; local credentials are never sent to a remote host.",
        }
    try:
        return {
            "ok": True,
            "running": service_health(config, host_home),
            "url": service_base_url(config, host_home),
            "error": "",
        }
    except ServiceCredentialInvalid as error:
        return {"ok": False, "running": False, "url": DEFAULT_BASE_URL, "error": str(error)}
    except ServiceCredentialMissing as error:
        return {"ok": True, "running": False, "url": DEFAULT_BASE_URL, "error": str(error)}


def opencode_binary(host_home):
    """Find the OpenCode CLI without assuming it is on the daemon PATH."""
    candidates = [
        os.environ.get("ORQELLA_OPENCODE_BIN"),
        shutil.which("opencode"),
        os.path.join(host_home, ".local", "share", "mise", "shims", "opencode"),
        os.path.join(host_home, ".local", "bin", "opencode"),
        "/usr/local/bin/opencode",
        "/usr/bin/opencode",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def sanitized_env(host_home):
    env = os.environ.copy()
    for key in ("ORQELLA_MODULE_TOKEN", "ORQELLA_MODULE_CONFIG"):
        env.pop(key, None)
    env["HOME"] = host_home
    return env


def ensure_service(config, host_home):
    """Reuse a running OpenCode service, or start the shared one on demand."""
    if configured_base_url(config) and configured_base_url(config) != DEFAULT_BASE_URL:
        return
    key = configured_base_url(config) or DEFAULT_BASE_URL
    if time.monotonic() < _service_ready_until.get(key, 0.0) and service_health(config, host_home):
        return
    with _service_lock:
        if service_health(config, host_home):
            _service_ready_until[key] = time.monotonic() + SERVICE_READY_SECONDS
            return
        binary = opencode_binary(host_home)
        if not binary:
            raise RuntimeError(
                "OpenCode is not installed or not on PATH. Install OpenCode, "
                "or set the harness base_url to an existing OpenCode service."
            )
        try:
            result = subprocess.run(
                [binary, "service", "start"],
                capture_output=True,
                text=True,
                timeout=SERVICE_START_TIMEOUT,
                env=sanitized_env(host_home),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"could not start the OpenCode service: {error}") from error
        deadline = time.monotonic() + SERVICE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if service_health(config, host_home):
                _service_ready_until[key] = time.monotonic() + SERVICE_READY_SECONDS
                return
            time.sleep(0.5)
        detail = (result.stderr or result.stdout or "").strip()
        message = "OpenCode service did not become reachable."
        if detail:
            message += f" {detail[-800:]}"
        else:
            message += " Try `opencode service restart`."
        raise RuntimeError(message)


def basic_auth(config, host_home):
    if not configured_base_is_local(config):
        raise RuntimeError(
            "refusing to send the local OpenCode service password to a non-loopback base_url"
        )
    service, _path = service_config(config, host_home)
    password = service.get("password") or ""
    return base64.b64encode(f"opencode:{password}".encode()).decode()


def opencode_api(config, host_home, method, path, body=None, accept=None):
    ensure_service(config, host_home)
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Basic " + basic_auth(config, host_home),
        "Content-Type": "application/json",
    }
    if accept:
        headers["Accept"] = accept
    request = Request(
        service_base_url(config, host_home) + path,
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


def activity(config, host_home, limit=200):
    """Read native session activity without starting the service.

    ACP has no live status notification, so OpenCode sessions adopted from
    history otherwise look idle while they are busy in the native app. This is a
    read-only hint; failures are silent and never start a process.
    """
    if not configured_base_is_local(config):
        return {}
    try:
        base = service_base_url(config, host_home)
        request = Request(
            base + f"/api/session?limit={max(1, min(200, int(limit)))}",
            headers={"Authorization": "Basic " + basic_auth(config, host_home)},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode() or "{}")
    except Exception:
        return {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    result = {}
    for session in rows:
        if not isinstance(session, dict) or not session.get("id"):
            continue
        times = session.get("time") or {}
        try:
            busy = int(times.get("busy") or 0)
            idle = int(times.get("idle") or 0)
            updated = int(times.get("updated") or 0)
        except (TypeError, ValueError):
            busy, idle, updated = 0, 0, 0
        result[session["id"]] = {
            "active": busy > 0 and busy > idle,
            "updated": updated,
        }
    return result


def pair(config, host_home):
    result = opencode_api(config, host_home, "POST", "/api/pair", {})
    code = str(result.get("code") or "")
    if not code:
        raise RuntimeError("OpenCode did not return a pairing code")
    auth = opencode_api(
        config,
        host_home,
        "GET",
        "/auth/connect/" + quote(code, safe=""),
        accept="application/json",
    )
    token = str(auth.get("token") or "")
    if not token:
        raise RuntimeError("OpenCode did not return an authentication token")
    encoded = base64.urlsafe_b64encode(f"opencode:{token}".encode()).decode().rstrip("=")
    return {"code": code, "auth_token": encoded}
