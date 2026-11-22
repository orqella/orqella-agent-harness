"""Generic web frontend support for harnesses that ship their own workspace.

A registry entry may declare a `web` object:

    {"mount": "opencode", "url": "http://127.0.0.1:49374",
     "path": "/server/{server_key}/session/{session}", "handoff": "opencode"}

The module opens the configured Orqella proxy mount with `app.open`. `handoff`
selects an optional auth/start driver; OpenCode is currently the only driver,
but the UI and registry do not single out the OpenCode harness.
"""
import opencode


def web(entry):
    value = entry.get("web") if isinstance(entry, dict) else None
    if not isinstance(value, dict) or not value.get("mount"):
        return None
    return value


def _opencode_config(value):
    return {"base_url": value.get("url") or ""}


def status(entry, host_home):
    value = web(entry)
    if not value:
        return {"available": False, "running": False, "error": "this harness has no web workspace"}
    result = {
        "available": True,
        "running": True,
        "mount": value.get("mount"),
        "url": value.get("url") or "",
        "path": value.get("path") or "/",
        "handoff": value.get("handoff") or "",
        "error": "",
    }
    if value.get("handoff") == "opencode":
        service = opencode.service_status(_opencode_config(value), host_home)
        result["running"] = bool(service.get("running"))
        result["url"] = service.get("url") or result["url"]
        result["error"] = service.get("error") or ""
    return result


def start(entry, host_home):
    value = web(entry)
    if not value:
        return status(entry, host_home)
    if value.get("handoff") == "opencode":
        opencode.ensure_service(_opencode_config(value), host_home)
    return status(entry, host_home)


def pair(entry, host_home):
    value = web(entry)
    if value and value.get("handoff") == "opencode":
        return opencode.pair(_opencode_config(value), host_home)
    return {"auth_token": ""}
