"""Client for a harness's managed HTTP service.

OpenCode's ACP command always spawns a private ``serve --stdio`` process, so
prompts sent through ACP are not broadcast to the user's other OpenCode clients
even though they share a session store. This module talks to the managed
service instead, which is the same process the harness's own clients use.

The registry calls this transport ``opencode`` because the wire API is
OpenCode's; everything above it (sessions, transcripts, prompts, permissions)
is the module's generic event model.
"""
import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import opencode

MODEL_CACHE_SECONDS = 300
COMMAND_CACHE_SECONDS = 60
_model_cache: dict[str, tuple[float, dict]] = {}
_model_lock = threading.Lock()
_command_cache: dict[str, tuple[float, list]] = {}
_model_list_cache: dict[str, tuple[float, list]] = {}
_agent_cache: dict[str, tuple[float, list]] = {}
_command_lock = threading.Lock()
_list_lock = threading.Lock()


class ServiceError(RuntimeError):
    """The managed service rejected the request or is unreachable."""


def config(entry):
    web = (entry or {}).get("web") or {}
    return {"base_url": web.get("url") or ""}


def base_url(entry, host_home):
    return opencode.service_base_url(config(entry), host_home)


def request(entry, host_home, method, path, body=None, timeout=60, start=False):
    if start:
        return opencode.opencode_api(config(entry), host_home, method, path, body)
    if not opencode.configured_base_is_local(config(entry)):
        raise ServiceError("the harness service URL must be loopback")
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Basic " + opencode.basic_auth(config(entry), host_home),
        "Content-Type": "application/json",
    }
    request = Request(base_url(entry, host_home) + path, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            output = response.read().decode()
    except HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        raise ServiceError(detail[-800:] or f"harness service returned HTTP {error.code}") from error
    except Exception as error:
        raise ServiceError(f"could not reach the harness service: {error}") from error
    return json.loads(output) if output.strip() else {}


def events(entry, host_home):
    """Yield server-sent events from the managed service.

    The caller is responsible for reconnecting. Heartbeats and malformed frames
    are skipped so one bad line cannot tear down the stream.
    """
    if not opencode.configured_base_is_local(config(entry)):
        raise ServiceError("the harness service URL must be loopback")
    request = Request(
        base_url(entry, host_home) + "/api/event",
        headers={
            "Authorization": "Basic " + opencode.basic_auth(config(entry), host_home),
            "Accept": "text/event-stream",
        },
    )
    with urlopen(request, timeout=3600) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except ValueError:
                continue
            if isinstance(payload, dict):
                yield payload


def info(entry, host_home, session_id):
    return request(entry, host_home, "GET", f"/api/session/{quote(session_id, safe='')}").get("data") or {}


def list_sessions(entry, host_home, limit=200, directory=None):
    query = f"?limit={max(1, min(200, int(limit or 200)))}"
    if directory:
        query += "&directory=" + quote(directory, safe="")
    return request(entry, host_home, "GET", f"/api/session{query}").get("data") or []


def commands(entry, host_home, directory=""):
    """List custom slash commands registered with the managed service."""
    try:
        key = base_url(entry, host_home) + "|" + (directory or "")
    except Exception:
        return []
    now = time.monotonic()
    with _command_lock:
        cached = _command_cache.get(key)
    if cached and now - cached[0] < COMMAND_CACHE_SECONDS:
        return list(cached[1])
    path = "/api/command"
    if directory:
        path += "?location%5Bdirectory%5D=" + quote(directory, safe="")
    try:
        rows = request(entry, host_home, "GET", path).get("data") or []
    except Exception:
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lstrip("/")
        if not name:
            continue
        result.append(
            {
                "name": name,
                "description": str(row.get("description") or ""),
            }
        )
    with _command_lock:
        _command_cache[key] = (now, result)
    return list(result)


def _location(directory):
    if not directory:
        return ""
    return "?location%5Bdirectory%5D=" + quote(directory, safe="")


def models(entry, host_home, directory=""):
    """List enabled chat models available to a session."""
    try:
        key = base_url(entry, host_home) + "|" + (directory or "")
    except Exception:
        return []
    now = time.monotonic()
    with _list_lock:
        cached = _model_list_cache.get(key)
    if cached and now - cached[0] < COMMAND_CACHE_SECONDS:
        return list(cached[1])
    try:
        rows = request(
            entry, host_home, "GET", "/api/model" + _location(directory)
        ).get("data") or []
    except Exception:
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        if row.get("type") not in (None, "chat"):
            continue
        provider = str(row.get("providerID") or "")
        model_id = str(row.get("id") or "")
        if not provider or not model_id:
            continue
        name = str(row.get("name") or model_id)
        result.append(
            {
                "value": f"{provider}/{model_id}",
                "label": name,
                "description": provider,
            }
        )
    result.sort(key=lambda item: item["label"].lower())
    with _list_lock:
        _model_list_cache[key] = (now, result)
    return list(result)


def agents(entry, host_home, directory=""):
    """List primary agents available to a session."""
    try:
        key = base_url(entry, host_home) + "|" + (directory or "")
    except Exception:
        return []
    now = time.monotonic()
    with _list_lock:
        cached = _agent_cache.get(key)
    if cached and now - cached[0] < COMMAND_CACHE_SECONDS:
        return list(cached[1])
    try:
        rows = request(
            entry, host_home, "GET", "/api/agent" + _location(directory)
        ).get("data") or []
    except Exception:
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("mode") != "primary" or row.get("hidden"):
            continue
        agent_id = str(row.get("id") or "")
        if not agent_id:
            continue
        result.append(
            {
                "value": agent_id,
                "label": str(row.get("name") or agent_id),
                "description": str(row.get("description") or ""),
            }
        )
    result.sort(key=lambda item: item["label"].lower())
    with _list_lock:
        _agent_cache[key] = (now, result)
    return list(result)


def set_model(entry, host_home, session_id, provider_id, model_id, variant=None):
    model = {"providerID": provider_id, "id": model_id}
    if variant:
        model["variant"] = variant
    return request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/model",
        {"model": model},
        start=True,
    )


def set_agent(entry, host_home, session_id, agent_id):
    return request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/agent",
        {"agent": agent_id},
        start=True,
    )


def compact(entry, host_home, session_id):
    """Ask the managed service to compact a session's context."""
    return request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/compact",
        {},
        start=True,
    )


def command(entry, host_home, session_id, name, text="", images=None, delivery=None):
    """Run a registered slash command against a session."""
    body = {"name": name, "text": text or ""}
    if delivery:
        body["delivery"] = delivery
    files = []
    for image in images or []:
        if not isinstance(image, dict) or not image.get("data"):
            continue
        files.append(image_file(host_home, image))
    if files:
        body["files"] = files
    return request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/command",
        body,
        start=True,
    )


def create(entry, host_home, cwd, title=""):
    body = {"location": {"directory": cwd}}
    if title:
        body["title"] = title
    return request(entry, host_home, "POST", "/api/session", body, start=True).get("data") or {}


def healthy(entry, host_home):
    try:
        return opencode.service_health(config(entry), host_home)
    except Exception:
        return False


def ensure_mcp(entry, host_home, servers, start=True):
    """Install Orqella MCP servers into the managed OpenCode service.

    OpenCode's REST session API does not accept per-session MCP servers yet, so
    the harness adds its server to the service's dynamic MCP registry. The
    harness-specific name avoids replacing a user's own `orqella` entry.
    """
    installed = []
    for server in servers or []:
        name = str(server.get("name") or "").strip()
        command = str(server.get("command") or "").strip()
        args = [str(arg) for arg in (server.get("args") or [])]
        if not name or not command:
            continue
        environment = {}
        for item in server.get("env") or []:
            if isinstance(item, dict) and item.get("name"):
                environment[str(item["name"])] = str(item.get("value") or "")
        config = {
            "type": "local",
            "command": [command, *args],
            "environment": environment,
            "codemode": False,
        }
        request(
            entry,
            host_home,
            "PUT",
            f"/api/experimental/mcp/{quote(name, safe='')}",
            {"config": config},
            start=start,
        )
        installed.append(name)
    return installed


def remove_mcp(entry, host_home, name):
    request(
        entry,
        host_home,
        "DELETE",
        f"/api/experimental/mcp/{quote(name, safe='')}",
    )
    return True


def remove(entry, host_home, session_id):
    request(entry, host_home, "DELETE", f"/api/session/{quote(session_id, safe='')}")
    return True


def interrupt(entry, host_home, session_id):
    request(entry, host_home, "POST", f"/api/session/{quote(session_id, safe='')}/interrupt", {})
    return True


def prompt(entry, host_home, session_id, text, images=None, delivery=None):
    body = {"text": text, "resume": True}
    if delivery:
        body["delivery"] = delivery
    files = []
    for image in images or []:
        if not isinstance(image, dict) or not image.get("data"):
            continue
        files.append(image_file(host_home, image))
    if files:
        body["files"] = files
    return request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/prompt",
        body,
        start=True,
    )


def image_file(host_home, image):
    """Store an attached image where the managed service can inline it."""
    mime = str(image.get("mimeType") or "image/png")
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".png")
    directory = os.path.join(host_home, ".cache", "orqella", "agent-harness", "attachments")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{uuid.uuid4().hex}{extension}")
    try:
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(image["data"]))
    except (OSError, ValueError):
        return {"uri": f"data:{mime};base64,{image['data']}", "source": {"type": "inline"}}
    return {
        "uri": path,
        "name": os.path.basename(path),
        "source": {"type": "inline"},
    }


def message_page(entry, host_home, session_id, limit=200, cursor=None, order=None):
    """Read one page of a session's message log.

    Returns the rows and the cursor that continues toward older messages. A
    caller that wants the newest page asks for ``order="desc"``; a caller that
    wants the next older page passes the returned cursor without an order.
    """
    params = [f"limit={max(1, min(200, int(limit or 200)))}"]
    if cursor:
        params.append("cursor=" + quote(str(cursor), safe=""))
    elif order:
        params.append("order=" + quote(str(order), safe=""))
    path = f"/api/session/{quote(session_id, safe='')}/message?" + "&".join(params)
    payload = request(entry, host_home, "GET", path)
    rows = payload.get("data") or []
    next_cursor = str((payload.get("cursor") or {}).get("next") or "")
    return rows, next_cursor


def messages(entry, host_home, session_id, limit=200):
    """Read a session's full message log, following pagination cursors."""
    rows = []
    cursor = None
    for _ in range(40):
        page, cursor = message_page(
            entry,
            host_home,
            session_id,
            limit=limit,
            cursor=cursor,
            order=None if cursor else "asc",
        )
        rows.extend(page)
        if not cursor:
            break
    return rows


def reply_permission(entry, host_home, session_id, request_id, reply):
    request(
        entry,
        host_home,
        "POST",
        f"/api/session/{quote(session_id, safe='')}/permission/{quote(request_id, safe='')}/reply",
        {"reply": reply},
    )
    return True


def model_context(entry, host_home, model):
    """Return the context window for a model ref, cached briefly."""
    if not isinstance(model, dict):
        return 0
    provider = str(model.get("providerID") or "")
    model_id = str(model.get("id") or "")
    if not provider or not model_id:
        return 0
    key = base_url(entry, host_home)
    now = time.monotonic()
    with _model_lock:
        cached = _model_cache.get(key)
    if cached and now - cached[0] < MODEL_CACHE_SECONDS:
        return cached[1].get((provider, model_id), 0)
    index = {}
    try:
        rows = request(entry, host_home, "GET", "/api/model").get("data") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            limit = row.get("limit") or {}
            try:
                context = int(limit.get("context") or 0)
            except (TypeError, ValueError):
                context = 0
            index[(str(row.get("providerID") or ""), str(row.get("id") or ""))] = context
    except ServiceError:
        index = {}
    with _model_lock:
        _model_cache[key] = (now, index)
    return index.get((provider, model_id), 0)


def iso_time(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return ""
