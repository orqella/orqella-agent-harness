"""Harness registry.

A harness entry is data, never code: how to start the harness, which protocol
it speaks, and which optional integrations are allowed. ACP harnesses normally
speak the protocol over stdio; an entry may instead use the harness's managed
HTTP service when it has one, so the module and the harness's own clients share
one live session. Other harnesses speak ACP over stdio or run a one-shot
command.
"""
import json
import os
import shutil
import subprocess

KINDS = ("acp", "exec")
REGISTRY_VERSION = 1
ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")


class RegistryError(ValueError):
    """The registry could not be validated."""


BUILT_INS = [
    {
        "id": "opencode",
        "label": "OpenCode",
        "kind": "acp",
        "command": "opencode",
        "args": ["acp"],
        "transport": "opencode",
        "env": {},
        "login_hint": "Run `opencode` in a terminal and finish onboarding.",
        "web": {
            "mount": "opencode",
            "url": "http://127.0.0.1:49374",
            "path": "/server/{server_key}/session/{session}",
            "handoff": "opencode",
        },
        "delegate_fs": True,
        "delegate_terminal": True,
        "builtin": True,
    },
    {
        "id": "claude",
        "label": "Claude Code",
        "kind": "acp",
        "command": "claude-agent-acp",
        "args": [],
        "env": {},
        "fallback": {
            "command": "npx",
            "args": ["-y", "@agentclientprotocol/claude-agent-acp"],
        },
        "login_hint": "Run `claude` in a terminal and sign in.",
        "delegate_fs": True,
        "delegate_terminal": True,
        "builtin": True,
    },
    {
        "id": "codex",
        "label": "Codex CLI",
        "kind": "acp",
        "command": "codex-acp",
        "args": [],
        "env": {},
        "fallback": {
            "command": "npx",
            "args": ["-y", "@agentclientprotocol/codex-acp"],
        },
        "login_hint": "Run `codex login` in a terminal.",
        "delegate_fs": True,
        "delegate_terminal": True,
        "builtin": True,
    },
    {
        "id": "gemini",
        "label": "Gemini CLI",
        "kind": "acp",
        "command": "gemini",
        "args": ["--acp"],
        "env": {},
        "login_hint": "Run `gemini` in a terminal and sign in.",
        "delegate_fs": True,
        "delegate_terminal": True,
        "builtin": True,
    },
    {
        "id": "custom",
        "label": "Custom command",
        "kind": "exec",
        "command": "",
        "args": ["{prompt}"],
        "env": {},
        "login_hint": "Configure the command in harness settings.",
        "delegate_fs": False,
        "delegate_terminal": False,
        "builtin": True,
    },
]


def default_registry(default="opencode"):
    return {
        "version": REGISTRY_VERSION,
        "default": default,
        "harnesses": [dict(entry) for entry in BUILT_INS],
    }


def normalize(value, default="opencode"):
    """Validate and normalize a registry document.

    Unknown fields are dropped so a malformed or stale document cannot smuggle
    behavior into the sidecar. Built-in ids keep their protocol kind so a user
    cannot turn OpenCode into an ACP process by editing JSON by hand.
    """
    if not isinstance(value, dict):
        raise RegistryError("registry must be an object")
    raw_harnesses = value.get("harnesses")
    if not isinstance(raw_harnesses, list):
        raise RegistryError("registry.harnesses must be a list")
    builtin_kinds = {entry["id"]: entry["kind"] for entry in BUILT_INS}
    seen = set()
    harnesses = []
    for raw in raw_harnesses:
        entry = _normalize_entry(raw, builtin_kinds)
        if entry["id"] in seen:
            raise RegistryError(f"duplicate harness id `{entry['id']}`")
        seen.add(entry["id"])
        harnesses.append(entry)
    if not harnesses:
        harnesses = [dict(entry) for entry in BUILT_INS]
    selected = str(value.get("default") or "").strip()
    if selected not in seen:
        selected = harnesses[0]["id"]
    return {"version": REGISTRY_VERSION, "default": selected, "harnesses": harnesses}


def _normalize_entry(raw, builtin_kinds):
    if not isinstance(raw, dict):
        raise RegistryError("each harness must be an object")
    raw_id = str(raw.get("id") or "")
    harness_id = raw_id.strip().lower()
    if raw_id != raw_id.strip():
        raise RegistryError("harness id may not start or end with whitespace")
    if not harness_id or len(harness_id) > 64:
        raise RegistryError("harness id is required and must be 64 characters or fewer")
    if not set(harness_id) <= ID_CHARS or not harness_id[0].isalnum():
        raise RegistryError(
            f"harness id `{harness_id}` must start with a letter or digit and use "
            "letters, digits, `.`, `_`, or `-`"
        )
    kind = str(raw.get("kind") or "acp").strip().lower()
    if kind not in KINDS:
        raise RegistryError(f"harness `{harness_id}` has an unknown kind `{kind}`")
    if harness_id in builtin_kinds:
        kind = builtin_kinds[harness_id]
    command = str(raw.get("command") or "").strip()
    if not command and harness_id not in builtin_kinds:
        raise RegistryError(f"harness `{harness_id}` needs a command")
    transport = str(raw.get("transport") or "").strip().lower()
    if not transport:
        transport = "acp"
    if transport not in ("acp", "opencode"):
        raise RegistryError(f"harness `{harness_id}` has an unknown transport `{transport}`")
    if kind == "exec":
        transport = "acp"
    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise RegistryError(f"harness `{harness_id}` args must be a list of strings")
    env = raw.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in env.items()
    ):
        raise RegistryError(f"harness `{harness_id}` env must be a string map")
    web = raw.get("web")
    if "transport" not in raw and harness_id in builtin_kinds:
        default = next((item for item in BUILT_INS if item["id"] == harness_id), None)
        if default and default.get("transport"):
            transport = default["transport"]
    if "web" not in raw and harness_id in builtin_kinds:
        default = next((item for item in BUILT_INS if item["id"] == harness_id), None)
        if default and default.get("web"):
            web = dict(default["web"])
    if web is not None:
        if not isinstance(web, dict):
            raise RegistryError(f"harness `{harness_id}` web must be an object")
        web = {
            "mount": str(web.get("mount") or "").strip(),
            "url": str(web.get("url") or "").strip(),
            "path": str(web.get("path") or "").strip(),
            "handoff": str(web.get("handoff") or "").strip().lower(),
        }
        if not web["mount"]:
            raise RegistryError(f"harness `{harness_id}` web requires a mount")
        if not web["path"].startswith("/") if web["path"] else False:
            raise RegistryError(f"harness `{harness_id}` web path must start with /")
    fallback = raw.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, dict):
            raise RegistryError(f"harness `{harness_id}` fallback must be an object")
        fallback = {
            "command": str(fallback.get("command") or "").strip(),
            "args": fallback.get("args") or [],
        }
        if not fallback["command"] or not isinstance(fallback["args"], list):
            raise RegistryError(f"harness `{harness_id}` fallback is incomplete")
        if not all(isinstance(item, str) for item in fallback["args"]):
            raise RegistryError(f"harness `{harness_id}` fallback args must be strings")
    label = str(raw.get("label") or harness_id).strip() or harness_id
    return {
        "id": harness_id,
        "label": label[:80],
        "kind": kind,
        "command": command,
        "args": [str(item) for item in args],
        "env": {str(key): str(item) for key, item in env.items()},
        "fallback": fallback,
        "transport": transport,
        "web": web,
        "login_hint": str(raw.get("login_hint") or "").strip()[:400],
        "delegate_fs": bool(raw.get("delegate_fs", False)),
        "delegate_terminal": bool(raw.get("delegate_terminal", False)),
        "builtin": harness_id in builtin_kinds,
        "enabled": bool(raw.get("enabled", True)),
    }


def resolve_command(entry, allow_fallback=False):
    """Return (command, args, source) or (None, [], reason)."""
    command = resolve_binary(entry.get("command") or "")
    if command:
        return command, list(entry.get("args") or []), "path"
    fallback = entry.get("fallback") or {}
    if allow_fallback and fallback:
        fallback_command = resolve_binary(fallback.get("command") or "")
        if fallback_command:
            return fallback_command, list(fallback.get("args") or []), "fallback"
    hint = entry.get("login_hint") or ""
    reason = f"`{entry.get('command')}` was not found on PATH."
    if fallback and not allow_fallback:
        reason += " Enable the adapter fallback in module settings to run it with npx."
    if hint:
        reason += f" {hint}"
    return None, [], reason


def resolve_binary(command):
    if not command:
        return None
    if os.path.isabs(command):
        return command if os.path.isfile(command) and os.access(command, os.X_OK) else None
    found = shutil.which(command)
    if found:
        return found
    home = os.environ.get("ORQELLA_HOST_HOME") or os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".local", "bin", command),
        os.path.join(home, ".local", "share", "mise", "shims", command),
        os.path.join(home, ".local", "share", "mise", "command-wrappers", "bin", command),
        os.path.join("/usr/local/bin", command),
        os.path.join("/usr/bin", command),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe(entry, allow_fallback=False):
    command, args, source = resolve_command(entry, allow_fallback=allow_fallback)
    result = {
        "id": entry.get("id"),
        "installed": bool(command),
        "source": source,
        "command": command or entry.get("command"),
        "version": None,
        "error": "",
    }
    if not command:
        result["error"] = source
        return result
    candidates = [
        [command, "--version"],
        [command, "version"],
    ]
    if args:
        candidates.append([command, *args, "--version"])
    for invocation in candidates:
        try:
            completed = subprocess.run(
                invocation,
                capture_output=True,
                text=True,
                timeout=8,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if output and completed.returncode == 0:
            result["version"] = output.splitlines()[0][:160]
            break
    return result


def public_registry(value):
    """Registry shape sent to the UI: no resolved secrets or absolute hosts."""
    return {
        "version": value.get("version", REGISTRY_VERSION),
        "default": value.get("default"),
        "harnesses": [
            {
                "id": entry.get("id"),
                "label": entry.get("label") or entry.get("id"),
                "kind": entry.get("kind"),
                "command": entry.get("command") or "",
                "args": entry.get("args") or [],
                "env": entry.get("env") or {},
                "fallback": entry.get("fallback"),
                "transport": entry.get("transport") or "acp",
                "web": entry.get("web"),
                "login_hint": entry.get("login_hint") or "",
                "delegate_fs": bool(entry.get("delegate_fs")),
                "delegate_terminal": bool(entry.get("delegate_terminal")),
                "builtin": bool(entry.get("builtin")),
                "enabled": entry.get("enabled", True) is not False,
            }
            for entry in value.get("harnesses", [])
        ],
    }


def dump(value):
    return json.dumps(public_registry(value), indent=2) + "\n"
