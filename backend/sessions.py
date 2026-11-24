"""Harness session manager: registry, normalized events, transcripts, drivers."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

import opencode_service
import registry as registry_module
from acp import AcpConnection, AcpError, AcpUnavailable, Terminal, _within, sanitized_env
from opencode import activity as opencode_activity

SESSION_VERSION = 1
EVENT_MEMORY_LIMIT = 5000
TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
CONTENT_MAX_BYTES = 8 * 1024 * 1024
IDLE_CLOSE_SECONDS = 15 * 60
PROMPT_SESSION_KEYS = ("sessionID", "session_id", "thread_id", "conversation_id")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_id(harness_id):
    return f"{harness_id}-{uuid.uuid4().hex[:16]}"


class Store:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()

    def path(self, name):
        return os.path.join(self.root, name)

    def read_json(self, name, default):
        try:
            with open(self.path(name), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            return default

    def write_json(self, name, value):
        os.makedirs(self.root, exist_ok=True)
        target = self.path(name)
        temporary = target + ".tmp"
        with self.lock:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)

    def append_jsonl(self, name, value):
        os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
        with self.lock:
            with open(self.path(name), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")

    def read_jsonl(self, name, limit=None):
        rows = []
        try:
            with open(self.path(name), "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
                    if limit and len(rows) >= limit:
                        break
        except FileNotFoundError:
            pass
        return rows


class ExecDriver:
    """One-shot command driver for arbitrary harnesses."""

    kind = "exec"

    def __init__(self, manager, entry):
        self.manager = manager
        self.entry = entry
        self.process = None
        self.session_id = None

    def configured(self):
        return bool(self.entry.get("command"))

    def start(self, prompt, cwd):
        from registry import resolve_command

        command, args, source = resolve_command(self.entry, allow_fallback=False)
        if not command:
            raise AcpUnavailable(source or "the custom harness command is not configured")
        replaced = False
        final = []
        for argument in args:
            if "{prompt}" in argument:
                final.append(argument.replace("{prompt}", prompt))
                replaced = True
            else:
                final.append(argument)
        if not replaced:
            final.append(prompt)
        env = sanitized_env(self.manager.host_home())
        for key, value in (self.entry.get("env") or {}).items():
            from acp import resolve_env_value

            resolved = resolve_env_value(value)
            if resolved is not None:
                env[key] = resolved
        self.process = subprocess.Popen(
            [command, *final],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        return self.process

    def cancel(self):
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, 15)
            except OSError:
                pass


class SessionManager:
    def __init__(self, data_dir, config=None):
        self.module_config = config or {}
        self.data_dir = data_dir
        self.store = Store(data_dir)
        self.host_home_dir = os.environ.get("ORQELLA_HOST_HOME") or os.path.expanduser("~")
        self.lock = threading.RLock()
        self.log_path = self.store.path("sidecar.log")
        self.registry = self._load_registry()
        self.sessions = {}
        self.events = {}
        self.sequences = {}
        self.subscribers = {}
        self.pending_permissions = {}
        self.terminals = {}
        self.connections = {}
        self.exec_drivers = {}
        self._probe_cache = {}
        self._activity_cache = {"at": 0.0, "value": {}, "available": False}
        self._hidden_rows, self.hidden_sessions = self._load_hidden()
        self._refreshing = set()
        self._service_watch_lock = threading.Lock()
        self._service_threads = {}
        self._service_chunks = {}
        self._service_seen = {}
        self._service_tool_inputs = {}
        self._service_ready = set()
        self._load_sessions()
        self._prune_transcripts()
        if any(self.service_transport(self.entry(row.get("harness"))) for row in self.sessions.values()):
            self._ensure_service_watch()

    # -- configuration -----------------------------------------------------

    def _load_registry(self):
        raw = self.store.read_json("harnesses.json", None)
        default = str(self.module_config.get("default_harness") or "opencode").strip()
        if raw is None:
            value = registry_module.normalize(
                registry_module.default_registry(default or "opencode"),
                default or "opencode",
            )
            self.store.write_json("harnesses.json", value)
            return value
        try:
            return registry_module.normalize(raw, default or "opencode")
        except registry_module.RegistryError as error:
            self.log(f"registry rejected: {error}")
            value = registry_module.default_registry(default or "opencode")
            self.store.write_json("harnesses.json", value)
            return value

    def registry_value(self):
        with self.lock:
            return registry_module.public_registry(self.registry)

    def set_registry(self, value):
        with self.lock:
            normalized = registry_module.normalize(value, self.registry.get("default") or "opencode")
            self.registry = normalized
            self._probe_cache.clear()
            self.store.write_json("harnesses.json", normalized)
            for harness_id, connection in list(self.connections.items()):
                if harness_id not in {entry["id"] for entry in normalized["harnesses"]}:
                    connection.close()
                    self.connections.pop(harness_id, None)
            return registry_module.public_registry(normalized)

    def entry(self, harness_id):
        for item in self.registry.get("harnesses", []):
            if item.get("id") == harness_id:
                return item
        return None

    def service_transport(self, entry):
        return bool(entry) and entry.get("kind") == "acp" and entry.get("transport") == "opencode"

    def probe(self, harness_id):
        entry = self.entry(harness_id)
        if not entry:
            raise KeyError(harness_id)
        cached = self._probe_cache.get(harness_id)
        if cached and time.monotonic() - cached["at"] < 30:
            result = dict(cached["value"])
        else:
            result = registry_module.probe(entry, self.allow_adapter_install())
            self._probe_cache[harness_id] = {"at": time.monotonic(), "value": dict(result)}
        result["running"] = harness_id in self.connections and self.connections[harness_id].alive()
        return result

    def harnesses(self):
        output = []
        for entry in self.registry.get("harnesses", []):
            record = {
                **entry,
                "sessions": len([s for s in self.sessions.values() if s.get("harness") == entry["id"]]),
            }
            try:
                record.update({key: value for key, value in self.probe(entry["id"]).items() if key not in record})
            except KeyError:
                pass
            output.append(record)
        return {"default": self.registry.get("default"), "harnesses": output}

    def allow_adapter_install(self):
        return bool(self.module_config.get("allow_adapter_install"))

    def retention_days(self):
        try:
            return max(0, min(365, int(self.module_config.get("transcript_retention_days", 30))))
        except (TypeError, ValueError):
            return 30

    def max_sessions(self):
        try:
            return max(1, min(12, int(self.module_config.get("max_concurrent_sessions", 4))))
        except (TypeError, ValueError):
            return 4

    # -- session records ---------------------------------------------------

    def _load_hidden(self):
        raw = self.store.read_json("hidden_sessions.json", {"sessions": []})
        rows = raw.get("sessions") if isinstance(raw, dict) else []
        values = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("harness") or not row.get("session_id"):
                continue
            values.append(row)
        return values, {f"{row['harness']}:{row['session_id']}" for row in values}

    def _hide_session(self, harness_id, harness_session_id):
        key = f"{harness_id}:{harness_session_id}"
        if not harness_session_id or key in self.hidden_sessions:
            return
        self.hidden_sessions.add(key)
        self._hidden_rows.append({
            "harness": harness_id,
            "session_id": harness_session_id,
            "at": now(),
        })
        self.store.write_json("hidden_sessions.json", {"sessions": self._hidden_rows})

    def _transcript_sequence(self, transcript):
        """Return the highest seq already persisted for a session."""
        if not transcript:
            return 0
        path = os.path.join(self.data_dir, transcript)
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return 0
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            try:
                return int(event.get("seq") or 0)
            except (TypeError, ValueError):
                continue
        return 0

    def _load_sessions(self):
        raw = self.store.read_json("sessions.json", {"version": SESSION_VERSION, "sessions": []})
        rows = raw.get("sessions") if isinstance(raw, dict) else []
        for row in rows or []:
            if isinstance(row, dict) and row.get("id") and not row.get("native"):
                row["status"] = "idle" if row.get("status") in {"working", "starting", "idle", "detached"} else row.get("status", "idle")
                if self.service_transport(self.entry(row.get("harness"))):
                    row["service_needs_sync"] = True
                stored = int(row.get("last_seq") or 0)
                persisted = self._transcript_sequence(row.get("transcript"))
                row["last_seq"] = max(stored, persisted)
                self.sessions[row["id"]] = row
                self.sequences[row["id"]] = row["last_seq"]

    def _save_sessions(self):
        self.store.write_json(
            "sessions.json",
            {"version": SESSION_VERSION, "sessions": sorted(self.sessions.values(), key=lambda row: row.get("updated_at", ""), reverse=True)},
        )

    def _prune_transcripts(self):
        days = self.retention_days()
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        transcripts = os.path.join(self.data_dir, "transcripts")
        if not os.path.isdir(transcripts):
            return
        for name in os.listdir(transcripts):
            path = os.path.join(transcripts, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    for row in self.sessions.values():
                        if row.get("transcript") == f"transcripts/{name}":
                            row["transcript"] = ""
            except OSError:
                continue
        self._save_sessions()

    def create(self, harness_id, cwd, title="", additional_directories=None):
        entry = self.entry(harness_id)
        if not entry:
            raise ValueError(f"unknown harness `{harness_id}`")
        if not entry.get("enabled", True):
            raise ValueError(f"harness `{harness_id}` is disabled")
        cwd = self.normalize_cwd(cwd)
        if entry["kind"] == "acp":
            with self.lock:
                live = [s for s in self.sessions.values() if s.get("status") in {"starting", "working", "waiting", "idle"}]
                if len(live) >= self.max_sessions():
                    raise ValueError(
                        f"{len(live)} sessions are already open; close one or raise the concurrency limit"
                    )
            if self.service_transport(entry):
                info = opencode_service.create(entry, self.host_home_dir, cwd, title)
                session_id = str(info.get("id") or "")
                if not session_id:
                    raise ValueError("the harness service did not return a session id")
                row = self._new_row(
                    harness_id,
                    session_id,
                    cwd,
                    title or info.get("title") or "New session",
                )
                row["root_set"] = [cwd]
                self._ensure_service_watch()
                self._emit(row["id"], {"type": "status", "status": "idle"})
                return self.public_session(row)
            connection = self.connection(harness_id)
            result = connection.new_session(cwd, additional_directories)
            row = self._new_row(harness_id, result["sessionId"], cwd, title)
            row["capabilities"] = connection.capabilities
            row["agent_info"] = connection.agent_info
            self._apply_session_state(row, result)
            self._emit(row["id"], {"type": "status", "status": "idle"})
            return self.public_session(row)
        if entry["kind"] == "exec":
            row = self._new_row(harness_id, "", cwd, title)
            self._emit(row["id"], {"type": "status", "status": "idle"})
            return self.public_session(row)
        raise ValueError(f"harness `{harness_id}` has an unsupported kind")

    def adopt(self, harness_id, harness_session_id, cwd="", title=""):
        """Index a session the harness already knows about.

        The daemon's conversation viewer carries `{root}`; ACP history rows carry
        their own cwd. A local row is registered before loading so replayed
        history has a transcript to land in. Load is preferred for a new UI
        session because it replays the conversation; resume is for reconnecting
        to a session whose transcript this module already has.
        """
        entry = self.entry(harness_id)
        if not entry or entry.get("kind") != "acp":
            raise ValueError(f"harness `{harness_id}` is not an ACP harness")
        if f"{harness_id}:{harness_session_id}" in self.hidden_sessions:
            raise ValueError("this session was deleted")
        existing = self.find_by_harness_session(harness_session_id)
        if existing:
            return self.public_session(existing)
        if self.service_transport(entry):
            info = opencode_service.info(entry, self.host_home_dir, harness_session_id)
            if not info.get("id"):
                raise ValueError("the harness service does not know this session")
            cwd = self.normalize_cwd(
                cwd or ((info.get("location") or {}).get("directory")) or self.host_home()
            )
            row = self._new_row(
                harness_id,
                harness_session_id,
                cwd,
                title or info.get("title") or "Imported session",
            )
            row["root_set"] = [cwd]
            self._ensure_service_watch()
            try:
                rows, cursor = opencode_service.message_page(
                    entry,
                    self.host_home_dir,
                    harness_session_id,
                    limit=200,
                    order="desc",
                )
                rows.reverse()
                row["history_cursor"] = cursor or ""
                self._replay_service_messages(row["id"], rows)
            except Exception:
                with self.lock:
                    self.sessions.pop(row["id"], None)
                    self.events.pop(row["id"], None)
                    self.sequences.pop(row["id"], None)
                    if row.get("transcript"):
                        try:
                            os.remove(os.path.join(self.data_dir, row["transcript"]))
                        except OSError:
                            pass
                self._save_sessions()
                raise
            self.set_status(row["id"], "idle")
            self._save_sessions()
            return self.public_session(row)
        cwd = self.normalize_cwd(cwd or self.host_home())
        connection = self.connection(harness_id)
        if not title:
            try:
                page = connection.list_sessions() or {}
                for session in page.get("sessions") or []:
                    if session.get("sessionId") == harness_session_id:
                        title = session.get("title") or title
                        break
            except (AcpError, ValueError):
                pass
        can_load = bool(connection.capabilities.get("loadSession"))
        can_resume = connection.session_capabilities().get("resume") is not None
        if not can_load and not can_resume:
            raise ValueError("this harness cannot reopen existing sessions")
        row = self._new_row(harness_id, harness_session_id, cwd, title or "Imported session")
        row["agent_info"] = connection.agent_info
        try:
            if can_load:
                result = connection.load_session(harness_session_id, cwd)
            else:
                result = connection.resume_session(harness_session_id, cwd)
        except AcpError as error:
            with self.lock:
                self.sessions.pop(row["id"], None)
                self.events.pop(row["id"], None)
                self.sequences.pop(row["id"], None)
                if row.get("transcript"):
                    try:
                        os.remove(os.path.join(self.data_dir, row["transcript"]))
                    except OSError:
                        pass
                self._save_sessions()
            raise ValueError(str(error)) from error
        row["root_set"] = connection.roots(harness_session_id) or [cwd]
        self._apply_session_state(row, result)
        self._emit(row["id"], {"type": "status", "status": "idle"})
        self._save_sessions()
        return self.public_session(row)

    def discover(self, harness_id):
        """List sessions the harness already knows about (ACP session/list)."""
        entry = self.entry(harness_id)
        if not entry or entry.get("kind") != "acp" or not entry.get("enabled", True):
            return {"supported": False, "sessions": []}
        if self.service_transport(entry):
            try:
                rows = opencode_service.list_sessions(
                    entry, self.host_home_dir, limit=200
                )
            except Exception as error:  # noqa: BLE001 - discovery boundary
                return {"supported": False, "error": str(error), "sessions": []}
            output = []
            for session in rows:
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("id") or "")
                if not session_id:
                    continue
                if f"{harness_id}:{session_id}" in self.hidden_sessions:
                    continue
                times = session.get("time") or {}
                try:
                    busy = int(times.get("busy") or 0)
                    idle = int(times.get("idle") or 0)
                    updated = int(times.get("updated") or 0)
                except (TypeError, ValueError):
                    busy, idle, updated = 0, 0, 0
                active = busy > 0 and busy > idle
                output.append(
                    {
                        "harness": harness_id,
                        "session_id": session_id,
                        "cwd": str((session.get("location") or {}).get("directory") or ""),
                        "title": session.get("title") or "Untitled session",
                        "updated_at": opencode_service.iso_time(updated),
                        "discovered": True,
                        "status": "working" if active else "idle",
                        "native_active": active,
                        "native_updated": updated,
                    }
                )
            output.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
            return {"supported": True, "sessions": output[:500]}
        try:
            connection = self.connection(harness_id)
            if connection.session_capabilities().get("list") is None:
                return {"supported": False, "sessions": []}
            rows = []
            cursor = None
            for _ in range(5):
                page = connection.list_sessions(cursor=cursor) or {}
                for session in page.get("sessions") or []:
                    session_id = session.get("sessionId")
                    cwd = session.get("cwd") or ""
                    # ACP requires a cwd to load a session. Hide history whose
                    # workspace no longer exists instead of offering a row that
                    # cannot open.
                    if not session_id or not cwd or not os.path.isdir(cwd):
                        continue
                    if f"{harness_id}:{session_id}" in self.hidden_sessions:
                        continue
                    rows.append({
                        "harness": harness_id,
                        "session_id": session_id,
                        "cwd": cwd,
                        "title": session.get("title") or "Untitled session",
                        "updated_at": session.get("updatedAt") or "",
                        "discovered": True,
                    })
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        except (AcpError, ValueError) as error:
            return {"supported": False, "error": str(error), "sessions": []}
        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        entry = self.entry(harness_id)
        web = (entry or {}).get("web") or {}
        activity = self.native_activity() if web.get("handoff") == "opencode" else {}
        for row in rows:
            hint = activity.get(row["session_id"]) or {}
            row["native_active"] = bool(hint.get("active"))
            try:
                row["native_updated"] = int(hint.get("updated") or 0)
            except (TypeError, ValueError):
                row["native_updated"] = 0
        return {"supported": True, "sessions": rows[:500]}

    def _new_row(self, harness_id, harness_session_id, cwd, title):
        row = {
            "id": local_id(harness_id),
            "harness": harness_id,
            "harness_session_id": harness_session_id or "",
            "cwd": cwd,
            "title": title or "New session",
            "status": "idle",
            "native": False,
            "created_at": now(),
            "updated_at": now(),
            "transcript": "",
            "last_seq": 0,
            "root_set": [cwd],
        }
        with self.lock:
            self.sessions[row["id"]] = row
            row["transcript"] = f"transcripts/{row['id']}.jsonl"
            self._save_sessions()
        return row

    def _apply_session_state(self, row, result):
        if not isinstance(result, dict):
            return
        if result.get("configOptions"):
            row["config_options"] = result["configOptions"]
        if result.get("modes"):
            row["modes"] = result["modes"]
        if result.get("models"):
            row["models"] = result["models"]
        if result.get("currentModeId"):
            row["current_mode"] = result["currentModeId"]

    def native_activity(self, force=False):
        now = time.monotonic()
        if not force and now - self._activity_cache["at"] < 3:
            return self._activity_cache["value"]
        entry = next(
            (
                item
                for item in self.registry.get("harnesses", [])
                if (item.get("web") or {}).get("handoff") == "opencode"
                and item.get("enabled", True)
            ),
            None,
        )
        value = None
        if entry:
            web = entry.get("web") or {}
            value = opencode_activity({"base_url": web.get("url") or ""}, self.host_home_dir)
        available = isinstance(value, dict)
        self._activity_cache = {
            "at": now,
            "value": value if available else {},
            "available": available,
        }
        return self._activity_cache["value"]

    def native_activity_available(self):
        return bool(self._activity_cache.get("available"))

    def public_session(self, row):
        native = {}
        entry = self.entry(row.get("harness"))
        web = (entry or {}).get("web") or {}
        if web.get("handoff") == "opencode" and row.get("harness_session_id"):
            native = self.native_activity().get(row["harness_session_id"]) or {}
        native_active = bool(native.get("active"))
        try:
            native_updated = int(native.get("updated") or 0)
        except (TypeError, ValueError):
            native_updated = 0
        status = row.get("status") or "idle"
        if status == "detached":
            status = "idle"
        if native:
            # The managed service is authoritative for live activity: it knows
            # about turns started by other clients and survives sidecar restarts
            # where the stored status is only a cache.
            if native_active:
                if status != "waiting":
                    status = "working"
            elif status in {"working", "starting"}:
                status = "idle"
        return {
            "id": row["id"],
            "harness": row["harness"],
            "harness_session_id": row.get("harness_session_id") or "",
            "cwd": row.get("cwd") or "",
            "title": row.get("title") or "",
            "status": status,
            "native": bool(row.get("native")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "last_seq": self.sequences.get(row["id"], 0),
            "config_options": row.get("config_options") or [],
            "modes": row.get("modes") or [],
            "models": row.get("models") or [],
            "current_mode": row.get("current_mode") or "",
            "agent_info": row.get("agent_info") or {},
            "usage": row.get("usage") or None,
            "native_active": native_active,
            "native_updated": native_updated,
            "needs_sync": bool(row.get("service_needs_sync")),
            "has_history": bool(row.get("history_cursor")),
            "transcript": bool(row.get("transcript")),
        }

    def list(self, harness_id=None, limit=50):
        try:
            limit = max(1, min(200, int(limit or 50)))
        except (TypeError, ValueError):
            limit = 50
        with self.lock:
            rows = [
                self.public_session(row)
                for row in self.sessions.values()
                if not harness_id or row.get("harness") == harness_id
            ]
        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        return rows[:limit]

    def get(self, session_id):
        row = self.sessions.get(session_id)
        if not row:
            raise KeyError(session_id)
        return row

    def find_by_harness_session(self, harness_session_id):
        for row in self.sessions.values():
            if row.get("harness_session_id") == harness_session_id:
                return row
        return None

    def refresh(self, session_id):
        """Reload a session's history from the harness and replace the transcript."""
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        harness_session_id = row.get("harness_session_id") or ""
        if self.service_transport(entry):
            return self._refresh_service(row)
        if not entry or entry.get("kind") != "acp" or not harness_session_id:
            raise ValueError("this session cannot refresh its history")
        if row.get("status") in {"working", "waiting"}:
            raise ValueError("this session is busy")
        if session_id in self._refreshing:
            raise ValueError("this session is already refreshing")
        connection = self.connection(row["harness"])
        if not connection.capabilities.get("loadSession"):
            raise ValueError("this harness cannot replay session history")
        transcript = row.get("transcript") or ""
        path = os.path.join(self.data_dir, transcript) if transcript else ""
        backup = None
        if path:
            try:
                with open(path, "rb") as handle:
                    backup = handle.read()
            except OSError:
                backup = None
        self._refreshing.add(session_id)
        try:
            with self.lock:
                old_events = self.events.get(session_id, [])
                old_seq = self.sequences.get(session_id, 0)
                old_last = int(row.get("last_seq") or 0)
                self.events[session_id] = []
                self.sequences[session_id] = 0
                row["last_seq"] = 0
                if path:
                    try:
                        with open(path, "wb"):
                            pass
                    except OSError:
                        pass
            self._emit(session_id, {"type": "reset"})
            try:
                result = connection.load_session(harness_session_id, row.get("cwd") or self.host_home())
            except Exception:
                with self.lock:
                    self.events[session_id] = old_events
                    self.sequences[session_id] = old_seq
                    row["last_seq"] = old_last
                    if path and backup is not None:
                        try:
                            with open(path, "wb") as handle:
                                handle.write(backup)
                        except OSError:
                            pass
                raise
            if self.sequences.get(session_id, 0) <= 1:
                with self.lock:
                    self.events[session_id] = old_events
                    self.sequences[session_id] = old_seq
                    row["last_seq"] = old_last
                    if path and backup is not None:
                        try:
                            with open(path, "wb") as handle:
                                handle.write(backup)
                        except OSError:
                            pass
                raise ValueError("the harness did not replay session history")
            row["root_set"] = connection.roots(harness_session_id) or [row.get("cwd") or self.host_home()]
            row["agent_info"] = connection.agent_info
            self._apply_session_state(row, result)
            row.pop("service_needs_sync", None)
            self.set_status(session_id, "idle")
            self._save_sessions()
            return {"session": self.public_session(row), "events": self.sequences.get(session_id, 0)}
        finally:
            self._refreshing.discard(session_id)

    def _delete_native(self, harness_id, harness_session_id):
        if not harness_session_id:
            return False
        entry = self.entry(harness_id)
        if not entry:
            return False
        if self.service_transport(entry):
            try:
                return opencode_service.remove(entry, self.host_home_dir, harness_session_id)
            except Exception:
                return False
        if entry.get("kind") != "acp":
            return False
        try:
            connection = self.connections.get(harness_id) or self.connection(harness_id)
            return connection.delete_session(harness_session_id)
        except (AcpError, ValueError):
            return False

    def _remove_local(self, session_id):
        row = self.sessions.pop(session_id, None)
        self.events.pop(session_id, None)
        self.sequences.pop(session_id, None)
        self.subscribers.pop(session_id, None)
        if row and row.get("transcript"):
            try:
                os.remove(os.path.join(self.data_dir, row["transcript"]))
            except OSError:
                pass
        self._save_sessions()
        return row

    def delete(self, session_id, native=True):
        row = self.get(session_id)
        harness_session_id = row.get("harness_session_id") or ""
        native_deleted = self._delete_native(row["harness"], harness_session_id) if native else False
        if harness_session_id:
            # Hide even after a successful native delete so a cached
            # session/list cannot resurrect the conversation.
            self._hide_session(row["harness"], harness_session_id)
        with self.lock:
            self._remove_local(session_id)
        return {
            "deleted": session_id,
            "native_deleted": native_deleted,
        }

    def forget(self, harness_id, harness_session_id, native=True):
        existing = self.find_by_harness_session(harness_session_id)
        if existing:
            return self.delete(existing["id"], native=native)
        native_deleted = self._delete_native(harness_id, harness_session_id) if native else False
        self._hide_session(harness_id, harness_session_id)
        return {
            "deleted": harness_session_id,
            "native_deleted": native_deleted,
        }

    def transcript(self, session_id, since=0):
        row = self.get(session_id)
        if not row.get("transcript"):
            return []
        rows = self.store.read_jsonl(row["transcript"])
        return [event for event in rows if int(event.get("seq") or 0) > int(since or 0)]

    # -- events ------------------------------------------------------------

    def _emit(self, session_id, event, broadcast=True):
        row = self.sessions.get(session_id)
        if not row:
            return
        with self.lock:
            sequence = int(self.sequences.get(session_id, 0)) + 1
            self.sequences[session_id] = sequence
            event = {"seq": sequence, "at": now(), **event}
            events = self.events.setdefault(session_id, [])
            events.append(event)
            if len(events) > EVENT_MEMORY_LIMIT:
                del events[: len(events) - EVENT_MEMORY_LIMIT]
            row["last_seq"] = sequence
            row["updated_at"] = event["at"]
            transcript = row.get("transcript")
            if transcript:
                path = os.path.join(self.data_dir, transcript)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                if size < TRANSCRIPT_MAX_BYTES:
                    try:
                        self.store.append_jsonl(transcript, event)
                    except OSError:
                        pass
                else:
                    row["transcript"] = ""
            subscribers = list(self.subscribers.get(session_id, []))
        if not broadcast:
            return
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self, session_id):
        self.get(session_id)
        subscriber = queue.Queue(maxsize=2048)
        with self.lock:
            self.subscribers.setdefault(session_id, []).append(subscriber)
        return subscriber

    def unsubscribe(self, session_id, subscriber):
        with self.lock:
            values = self.subscribers.get(session_id, [])
            if subscriber in values:
                values.remove(subscriber)

    def set_status(self, session_id, status):
        row = self.sessions.get(session_id)
        if not row:
            return
        row["status"] = status
        self._emit(session_id, {"type": "status", "status": status})
        self._save_sessions()

    def update(self, harness_session_id, update):
        row = self.find_by_harness_session(harness_session_id)
        if not row:
            return
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate") or ""
        session_id = row["id"]
        if kind in {"agent_message_chunk", "agent_thought_chunk", "user_message_chunk"}:
            content = update.get("content") or {}
            role = {
                "agent_message_chunk": "agent",
                "agent_thought_chunk": "thought",
                "user_message_chunk": "user",
            }[kind]
            self._emit(
                session_id,
                {
                    "type": "message",
                    "role": role,
                    "message_id": update.get("messageId") or "",
                    "content": self._safe_content(content),
                },
            )
            return
        if kind in {"tool_call", "tool_call_update"}:
            self._emit(
                session_id,
                {
                    "type": "tool_call",
                    "call_id": update.get("toolCallId") or "",
                    "title": update.get("title"),
                    "name": update.get("name"),
                    "kind": update.get("kind"),
                    "status": update.get("status"),
                    "content": [self._safe_tool_content(item) for item in update.get("content") or []],
                    "locations": update.get("locations") or [],
                    "raw_input": self._cap(update.get("rawInput")),
                    "raw_output": self._cap(update.get("rawOutput")),
                },
            )
            return
        if kind == "plan":
            self._emit(session_id, {"type": "plan", "entries": update.get("entries") or []})
            return
        if kind == "config_option_update":
            row["config_options"] = update.get("configOptions") or []
            self._emit(session_id, {"type": "config", "config_options": row["config_options"]})
            return
        if kind == "current_mode_update":
            row["current_mode"] = update.get("currentModeId") or ""
            self._emit(session_id, {"type": "mode", "mode_id": row["current_mode"]})
            return
        if kind == "available_commands_update":
            commands = []
            for item in update.get("availableCommands") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip().lstrip("/")
                if not name:
                    continue
                command = {"name": name, "description": str(item.get("description") or "")}
                if isinstance(item.get("input"), dict):
                    command["input"] = item["input"]
                commands.append(command)
            row["commands"] = commands
            self._emit(session_id, {"type": "commands", "commands": commands})
            self._save_sessions()
            return
        if kind == "session_info_update":
            if update.get("title") is not None:
                row["title"] = str(update["title"])[:200]
            if update.get("updatedAt"):
                row["updated_at"] = update["updatedAt"]
            self._emit(session_id, {"type": "session_info", "title": row.get("title")})
            self._save_sessions()
            return
        if kind == "usage_update":
            try:
                used = int(update.get("used") or 0)
                size = int(update.get("size") or 0)
            except (TypeError, ValueError):
                used, size = 0, 0
            usage = {
                "used": used,
                "size": size,
                "cost": update.get("cost") if isinstance(update.get("cost"), dict) else None,
                "at": now(),
            }
            if size > 0:
                row["usage"] = usage
            self._emit(session_id, {"type": "usage", "usage": usage})
            self._save_sessions()
            return
        if "usage" in kind.lower() or "cost" in kind.lower():
            self._emit(session_id, {"type": "usage", "update": self._cap(update)})
            return
        if kind in {"notice", "session_notice"}:
            self._emit(session_id, {"type": "notice", "update": self._cap(update)})
            return
        self._emit(session_id, {"type": "update", "session_update": kind, "update": self._cap(update)})

    def _cap(self, value, limit=20000):
        try:
            text = json.dumps(value)
        except (TypeError, ValueError):
            return value
        if len(text) <= limit:
            return value
        return {"truncated": True, "preview": text[:limit]}

    def _safe_content(self, content):
        if not isinstance(content, dict):
            return {"type": "text", "text": str(content)[:20000]}
        if content.get("type") == "image" and isinstance(content.get("data"), str):
            if len(content["data"]) > CONTENT_MAX_BYTES * 2:
                return {"type": "text", "text": "[image omitted: too large]"}
            return content
        if content.get("type") == "text":
            return {"type": "text", "text": str(content.get("text") or "")[:200000]}
        return self._cap(content)

    def _safe_tool_content(self, item):
        if not isinstance(item, dict):
            return {"type": "content", "content": {"type": "text", "text": str(item)[:4000]}}
        if item.get("type") == "diff":
            return {
                "type": "diff",
                "path": item.get("path"),
                "oldText": (item.get("oldText") or "")[:200000],
                "newText": (item.get("newText") or "")[:200000],
            }
        if item.get("type") == "content":
            return {"type": "content", "content": self._safe_content(item.get("content") or {})}
        if item.get("type") == "terminal":
            return {"type": "terminal", "terminalId": item.get("terminalId")}
        return self._cap(item, 8000)

    def detached(self, harness_session_id, reason):
        row = self.find_by_harness_session(harness_session_id)
        if not row:
            return
        row["status"] = "detached"
        row["error"] = reason
        self._emit(row["id"], {"type": "error", "error": reason})
        self._emit(row["id"], {"type": "status", "status": "detached"})
        self._save_sessions()

    # -- shared harness service -------------------------------------------

    def _ensure_service_watch(self):
        for entry in self.registry.get("harnesses", []):
            if not self.service_transport(entry) or not entry.get("enabled", True):
                continue
            with self._service_watch_lock:
                thread = self._service_threads.get(entry["id"])
                if thread and thread.is_alive():
                    continue
                thread = threading.Thread(
                    target=self._service_event_loop,
                    args=(entry["id"],),
                    name=f"service-{entry['id']}",
                    daemon=True,
                )
                self._service_threads[entry["id"]] = thread
                thread.start()

    def _service_event_loop(self, harness_id):
        delay = 1.0
        while True:
            entry = self.entry(harness_id)
            if not self.service_transport(entry) or not entry.get("enabled", True):
                return
            try:
                for event in opencode_service.events(entry, self.host_home_dir):
                    delay = 1.0
                    if event.get("type") == "server.connected":
                        self._service_ready.add(harness_id)
                        self._mark_service_dirty(harness_id)
                        self._service_resync(harness_id)
                        continue
                    try:
                        self.handle_service_event(harness_id, event)
                    except Exception as error:  # noqa: BLE001 - stream boundary
                        self.log(f"service event for {harness_id}: {error}")
            except Exception as error:  # noqa: BLE001 - reconnect boundary
                self._service_ready.discard(harness_id)
                self.log(f"service stream for {harness_id}: {error}")
            time.sleep(delay)
            delay = min(delay * 2, 15.0)

    def _mark_service_dirty(self, harness_id):
        changed = False
        with self.lock:
            for row in self.sessions.values():
                if row.get("harness") == harness_id:
                    row["service_needs_sync"] = True
                    changed = True
        if changed:
            self._save_sessions()

    def _schedule_service_refresh(self, row):
        if not row.get("service_needs_sync") or row["id"] in self._refreshing:
            return

        def run():
            time.sleep(0.5)
            if row.get("status") in {"working", "waiting"}:
                return
            try:
                self._refresh_service(row)
            except Exception as error:  # noqa: BLE001 - background boundary
                self.log(f"service refresh for {row['id']}: {error}")

        threading.Thread(
            target=run,
            name=f"service-refresh-{row['id']}",
            daemon=True,
        ).start()

    def _service_resync(self, harness_id):
        entry = self.entry(harness_id)
        if not entry:
            return
        try:
            rows = opencode_service.list_sessions(entry, self.host_home_dir, limit=200)
        except Exception:
            return
        index = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
        for row in list(self.sessions.values()):
            if row.get("harness") != harness_id:
                continue
            info = index.get(row.get("harness_session_id") or "")
            if not info:
                continue
            times = info.get("time") or {}
            try:
                busy_ms = int(times.get("busy") or 0)
                idle_ms = int(times.get("idle") or 0)
                busy = busy_ms > 0 and busy_ms > idle_ms
            except (TypeError, ValueError):
                busy = False
            status = row.get("status")
            if busy and status not in {"working", "waiting"}:
                self.set_status(row["id"], "working")
            elif not busy and status in {"working", "waiting"}:
                self.set_status(row["id"], "idle")

    def _service_claim(self, session_id, message_id):
        """Claim a message id so a local echo and the stream cannot duplicate it."""
        if not message_id:
            return True
        key = (session_id, message_id)
        with self.lock:
            if key in self._service_seen:
                return False
            self._service_seen[key] = time.monotonic()
            if len(self._service_seen) > 20000:
                cutoff = time.monotonic() - 3600
                for item, at in list(self._service_seen.items()):
                    if at < cutoff:
                        self._service_seen.pop(item, None)
        return True

    def handle_service_event(self, harness_id, event):
        kind = str(event.get("type") or "")
        data = event.get("data")
        if not kind.startswith("session.") or not isinstance(data, dict):
            return
        harness_session_id = str(data.get("sessionID") or "")
        if not harness_session_id:
            return
        row = self.find_by_harness_session(harness_session_id)
        if not row or row.get("harness") != harness_id:
            return
        session_id = row["id"]
        if session_id in self._refreshing:
            return

        if kind == "session.inbox.enqueued":
            item = data.get("item") or {}
            if not isinstance(item, dict):
                return
            if item.get("type") == "compaction":
                message_id = str(data.get("inboxID") or "")
                if not self._service_claim(session_id, message_id):
                    return
                self._emit(
                    session_id,
                    {
                        "type": "message",
                        "role": "user",
                        "message_id": message_id,
                        "content": {"type": "text", "text": "/compact"},
                    },
                )
                return
            if item.get("type") != "user":
                return
            payload = item.get("payload") or {}
            message_id = str(data.get("inboxID") or "")
            if not self._service_claim(session_id, message_id):
                return
            self._emit(
                session_id,
                {
                    "type": "message",
                    "role": "user",
                    "message_id": message_id,
                    "content": {"type": "text", "text": str(payload.get("text") or "")},
                },
            )
            return

        if kind == "session.execution.started":
            if row.get("status") not in {"working", "waiting"}:
                self.set_status(session_id, "working")
            return
        if kind in {"session.execution.succeeded", "session.execution.interrupted"}:
            if row.get("status") != "idle":
                self._emit(
                    session_id,
                    {
                        "type": "turn_end",
                        "stop_reason": "end_turn" if kind.endswith("succeeded") else "interrupted",
                    },
                )
                self.set_status(session_id, "idle")
            self._schedule_service_refresh(row)
            return
        if kind == "session.execution.failed":
            self._emit(
                session_id,
                {
                    "type": "error",
                    "error": str(data.get("message") or data.get("error") or "The harness turn failed"),
                },
            )
            self.set_status(session_id, "failed")
            self._schedule_service_refresh(row)
            return

        if kind == "session.step.started":
            if isinstance(data.get("model"), dict):
                row["service_model"] = data["model"]
            if row.get("status") not in {"working", "waiting"}:
                self.set_status(session_id, "working")
            return
        if kind == "session.step.ended":
            self._service_usage(
                session_id,
                row,
                data.get("tokens"),
                row.get("service_model") or data.get("model"),
                data.get("cost"),
            )
            return

        if kind in {
            "session.text.started",
            "session.text.delta",
            "session.text.ended",
            "session.reasoning.started",
            "session.reasoning.delta",
            "session.reasoning.ended",
        }:
            self._service_text(session_id, kind, data)
            return

        if kind in {
            "session.tool.called",
            "session.tool.progress",
            "session.tool.success",
            "session.tool.failed",
        }:
            self._service_tool(session_id, data, kind.rsplit(".", 1)[-1])
            return

        if kind == "session.model.selected":
            model = data.get("model")
            if isinstance(model, dict):
                row["service_model"] = model
                label = f"{model.get('providerID')}/{model.get('id')}"
                if model.get("variant"):
                    label += f"#{model['variant']}"
                self._emit(
                    session_id,
                    {"type": "notice", "update": {"title": f"Model: {label}"}},
                )
                self._save_sessions()
            return
        if kind == "session.agent.selected":
            agent = str(data.get("agent") or "")
            if agent:
                self._emit(
                    session_id,
                    {"type": "notice", "update": {"title": f"Agent: {agent}"}},
                )
            return

        if kind == "session.renamed":
            title = str(data.get("title") or "").strip()
            if title:
                row["title"] = title[:200]
                self._emit(session_id, {"type": "session_info", "title": row["title"]})
                self._save_sessions()
            return

        if kind == "permission.asked":
            self._service_permission(session_id, row, data)
            return
        if kind == "permission.replied":
            request_id = str(data.get("id") or data.get("requestID") or "")
            with self.lock:
                pending = self.pending_permissions.pop(request_id, None)
            if pending:
                self._emit(
                    session_id,
                    {
                        "type": "permission_resolved",
                        "request_id": request_id,
                        "option_id": str(data.get("reply") or ""),
                    },
                )
                if row.get("status") == "waiting":
                    self.set_status(session_id, "working")
            return

        if kind == "session.retry.scheduled":
            self._emit(
                session_id,
                {
                    "type": "notice",
                    "update": {"title": "Retrying", "message": str(data.get("message") or data.get("reason") or "The harness will retry this turn.")},
                },
            )
            return
        if kind in {"session.compaction.started", "session.compaction.ended", "session.compaction.failed"}:
            labels = {
                "session.compaction.started": "Compacting context",
                "session.compaction.ended": "Context compacted",
                "session.compaction.failed": "Compaction failed",
            }
            self._emit(session_id, {"type": "notice", "update": {"title": labels[kind]}})
            return

    def _service_text(self, session_id, kind, data):
        role = "agent" if kind.startswith("session.text.") else "thought"
        segment = "text" if role == "agent" else "reason"
        message = str(data.get("assistantMessageID") or "")
        ordinal = str(data.get("ordinal") or 0)
        key = (session_id, message, segment, ordinal)
        message_id = f"{message}:{segment}:{ordinal}"
        if kind.endswith(".started"):
            self._service_chunks[key] = False
            return
        if kind.endswith(".delta"):
            text = str(data.get("delta") or "")
            self._service_chunks[key] = True
        else:
            text = str(data.get("text") or "")
            if self._service_chunks.pop(key, False):
                return
        if text:
            self._emit(
                session_id,
                {
                    "type": "message",
                    "role": role,
                    "message_id": message_id,
                    "content": {"type": "text", "text": text},
                },
            )

    def _service_tool(self, session_id, data, phase):
        event = self._service_tool_event(session_id, data, phase)
        if event:
            self._emit(session_id, event)

    def _service_tool_event(self, session_id, data, phase):
        name = str(data.get("name") or "")
        tool_input = data.get("input")
        if not isinstance(tool_input, dict):
            tool_input = (data.get("state") or {}).get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        key = (session_id, str(data.get("id") or ""))
        stored = self._service_tool_inputs.get(key) or {}
        if not name:
            name = str(stored.get("name") or "")
        if tool_input:
            self._service_tool_inputs[key] = {"name": name, "input": tool_input}
        else:
            tool_input = stored.get("input") or {}
            if not name:
                name = str(stored.get("name") or "")
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if not metadata and isinstance(state.get("metadata"), dict):
            metadata = state["metadata"]
        status = str(metadata.get("status") or state.get("status") or "")
        if phase == "called":
            tool_status = "in_progress"
        elif phase == "progress":
            tool_status = "in_progress"
        elif phase == "failed":
            tool_status = "failed"
        elif status == "failed":
            tool_status = "failed"
        elif phase in {"success", "replay"} and status not in {"pending", "in_progress", "running", ""}:
            tool_status = "completed"
        elif phase == "replay":
            tool_status = "in_progress" if status in {"pending", "in_progress", "running", ""} else "completed"
        else:
            tool_status = "completed"

        content = data.get("content")
        if content is None:
            content = state.get("content")
        if content is None and phase == "failed":
            content = [{"type": "text", "text": str(data.get("error") or metadata.get("error") or "")}]
        content = self._service_tool_content(content)

        path = ""
        for key in ("filePath", "file_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                path = value
                break
        locations = [{"path": path}] if path else []
        if name in {"edit", "patch", "multiedit"}:
            old = tool_input.get("oldString", tool_input.get("old_string", ""))
            new = tool_input.get("newString", tool_input.get("new_string", ""))
            if old is not None or new is not None:
                content = [
                    {
                        "type": "diff",
                        "path": path,
                        "oldText": str(old or "")[:200000],
                        "newText": str(new or "")[:200000],
                    }
                ] + content
        elif name == "write" and isinstance(tool_input.get("content"), str):
            content = [
                {
                    "type": "diff",
                    "path": path,
                    "oldText": "",
                    "newText": tool_input["content"][:200000],
                }
            ] + content

        raw_output = ""
        if phase in {"success", "failed", "replay"}:
            raw_output = "\n".join(
                str(item.get("text") or "")
                for item in (data.get("content") or state.get("content") or [])
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if not raw_output and phase == "failed":
                raw_output = str(data.get("error") or metadata.get("error") or "")

        return {
            "type": "tool_call",
            "call_id": str(data.get("id") or ""),
            "title": self._service_tool_title(name, tool_input),
            "name": name,
            "kind": self._service_tool_kind(name),
            "status": tool_status,
            "content": content,
            "locations": locations,
            "raw_input": self._cap(tool_input),
            "raw_output": self._cap(raw_output) if raw_output else None,
        }

    @staticmethod
    def _service_tool_kind(name):
        return {
            "shell": "execute",
            "bash": "execute",
            "edit": "edit",
            "write": "edit",
            "patch": "edit",
            "multiedit": "edit",
            "read": "read",
            "list": "read",
            "glob": "search",
            "grep": "search",
            "webfetch": "fetch",
            "websearch": "fetch",
            "fetch": "fetch",
            "task": "other",
        }.get(name, "other")

    @staticmethod
    def _service_tool_title(name, tool_input):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("filePath", "file_path", "path", "pattern", "url", "query"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return f"{name} {value}"
        return name

    def _service_tool_content(self, rows):
        output = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                output.append(
                    {
                        "type": "content",
                        "content": {"type": "text", "text": str(item.get("text") or "")[:200000]},
                    }
                )
                continue
            output.append(self._safe_tool_content(item))
        return output

    def _service_usage(self, session_id, row, tokens, model, cost):
        usage = self._service_usage_value(row, tokens, model, cost)
        if usage:
            self._emit(session_id, {"type": "usage", "usage": usage})

    def _service_usage_value(self, row, tokens, model, cost):
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

        def number(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        used = (
            number(tokens.get("input"))
            + number(tokens.get("output"))
            + number(tokens.get("reasoning"))
            + number(cache.get("read"))
            + number(cache.get("write"))
        )
        entry = self.entry(row.get("harness"))
        size = opencode_service.model_context(entry, self.host_home_dir, model) if entry else 0
        if size <= 0 or used <= 0:
            return None
        try:
            amount = float(cost or 0)
        except (TypeError, ValueError):
            amount = 0.0
        usage = {
            "used": used,
            "size": size,
            "cost": {"amount": amount, "currency": "USD"} if amount > 0 else None,
            "at": now(),
        }
        row["usage"] = usage
        return usage

    def _service_permission(self, session_id, row, data):
        request_id = str(data.get("id") or "")
        if not request_id:
            return
        action = str(data.get("action") or "permission")
        resources = [str(item) for item in (data.get("resources") or []) if isinstance(item, str)]
        title = action
        if resources:
            title += " · " + ", ".join(resources[:3])
            if len(resources) > 3:
                title += f" +{len(resources) - 3}"
        options = [
            {"option_id": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"option_id": "allow_always", "name": "Always allow", "kind": "allow_always"},
            {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
        ]
        with self.lock:
            self.pending_permissions[request_id] = {
                "session_id": session_id,
                "native": True,
                "request_id": request_id,
                "harness_session_id": row.get("harness_session_id") or "",
                "options": options,
            }
        self._emit(
            session_id,
            {
                "type": "permission_request",
                "request_id": request_id,
                "tool_call": {
                    "call_id": "",
                    "title": title,
                    "kind": "other",
                    "content": [],
                    "locations": [],
                    "raw_input": self._cap(data),
                },
                "options": options,
            },
        )
        self.set_status(session_id, "waiting")

    def _service_events(self, session_id, rows, include_turn_end=True, include_usage=True):
        events = []
        last_usage = None
        for message in rows or []:
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            message_id = str(message.get("id") or "")
            if kind == "compaction":
                events.append(
                    {
                        "type": "message",
                        "role": "user",
                        "message_id": message_id,
                        "content": {"type": "text", "text": "/compact"},
                    }
                )
                continue
            if kind == "user":
                text = str(message.get("text") or "")
                if text:
                    self._service_claim(session_id, message_id)
                    events.append(
                        {
                            "type": "message",
                            "role": "user",
                            "message_id": message_id,
                            "content": {"type": "text", "text": text},
                        }
                    )
                continue
            if kind == "assistant":
                for index, part in enumerate(message.get("content") or []):
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "reasoning":
                        events.append(
                            {
                                "type": "message",
                                "role": "thought",
                                "message_id": f"{message_id}:reason:{index}",
                                "content": {"type": "text", "text": str(part.get("text") or "")},
                            }
                        )
                    elif part_type == "text":
                        events.append(
                            {
                                "type": "message",
                                "role": "agent",
                                "message_id": f"{message_id}:text:{index}",
                                "content": {"type": "text", "text": str(part.get("text") or "")},
                            }
                        )
                    elif part_type == "tool":
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        tool_event = self._service_tool_event(
                            session_id,
                            {
                                "id": part.get("id") or f"{message_id}:tool:{index}",
                                "name": part.get("name"),
                                "state": state,
                                "input": state.get("input"),
                                "content": state.get("content"),
                                "metadata": state.get("metadata"),
                            },
                            "replay",
                        )
                        if tool_event:
                            events.append(tool_event)
                if message.get("tokens") and message.get("model"):
                    last_usage = message
                continue
            if kind == "idle" and include_turn_end:
                events.append(
                    {
                        "type": "turn_end",
                        "stop_reason": message.get("outcome") or "end_turn",
                    }
                )
        if include_usage and last_usage:
            row = self.sessions.get(session_id)
            if row:
                usage = self._service_usage_value(
                    row,
                    last_usage.get("tokens"),
                    last_usage.get("model"),
                    last_usage.get("cost"),
                )
                if usage:
                    events.append({"type": "usage", "usage": usage})
        return events

    def _replay_service_messages(self, session_id, rows):
        for event in self._service_events(session_id, rows):
            self._emit(session_id, event)

    def _refresh_service(self, row):
        session_id = row["id"]
        entry = self.entry(row["harness"])
        harness_session_id = row.get("harness_session_id") or ""
        if not entry or not harness_session_id:
            raise ValueError("this session cannot refresh its history")
        if row.get("status") in {"working", "waiting"}:
            raise ValueError("this session is busy")
        if session_id in self._refreshing:
            raise ValueError("this session is already refreshing")
        self._refreshing.add(session_id)
        try:
            rows, cursor = opencode_service.message_page(
                entry,
                self.host_home_dir,
                harness_session_id,
                limit=200,
                order="desc",
            )
            rows.reverse()
            with self.lock:
                old_events = self.events.get(session_id, [])
                old_seq = self.sequences.get(session_id, 0)
                old_last = int(row.get("last_seq") or 0)
                self.events[session_id] = []
                self.sequences[session_id] = 0
                row["last_seq"] = 0
                transcript = row.get("transcript")
                if transcript:
                    try:
                        with open(os.path.join(self.data_dir, transcript), "wb"):
                            pass
                    except OSError:
                        pass
            self._emit(session_id, {"type": "reset"})
            try:
                self._replay_service_messages(session_id, rows)
            except Exception:
                with self.lock:
                    self.events[session_id] = old_events
                    self.sequences[session_id] = old_seq
                    row["last_seq"] = old_last
                raise
            row["history_cursor"] = cursor or ""
            row.pop("service_needs_sync", None)
            self.set_status(session_id, "idle")
            self._save_sessions()
            return {"session": self.public_session(row), "events": self.sequences.get(session_id, 0)}
        finally:
            self._refreshing.discard(session_id)

    def history(self, session_id, cursor=""):
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        if not self.service_transport(entry):
            raise ValueError("this session cannot page history")
        harness_session_id = row.get("harness_session_id") or ""
        cursor = (cursor or row.get("history_cursor") or "").strip()
        if not cursor:
            return {"events": [], "has_more": False}
        if session_id in self._refreshing:
            raise ValueError("this session is refreshing")
        rows, next_cursor = opencode_service.message_page(
            entry,
            self.host_home_dir,
            harness_session_id,
            limit=200,
            cursor=cursor,
        )
        rows.reverse()
        events = self._service_events(
            session_id, rows, include_turn_end=False, include_usage=False
        )
        row["history_cursor"] = next_cursor or ""
        if events:
            self._emit(
                session_id,
                {
                    "type": "history",
                    "events": events,
                    "has_more": bool(next_cursor),
                },
                broadcast=False,
            )
        self._save_sessions()
        return {"events": events, "has_more": bool(next_cursor)}

    def _prompt_service(self, row, entry, text, images):
        harness_session_id = row.get("harness_session_id") or ""
        if not harness_session_id:
            raise ValueError("this session has no harness session id")
        self._ensure_service_watch()
        if row.get("harness") not in self._service_ready:
            row["service_needs_sync"] = True
        delivery = "steer" if row.get("status") in {"working", "waiting"} else None
        reply = opencode_service.prompt(
            entry,
            self.host_home_dir,
            harness_session_id,
            text or "Attached image",
            images,
            delivery,
        )
        message_id = str(((reply or {}).get("data") or {}).get("id") or "")
        if self._service_claim(row["id"], message_id):
            self._emit(
                row["id"],
                {
                    "type": "message",
                    "role": "user",
                    "message_id": message_id or "user-" + uuid.uuid4().hex[:10],
                    "content": {"type": "text", "text": text},
                },
            )
        if row.get("status") not in {"working", "waiting"}:
            self.set_status(row["id"], "working")
        self._save_sessions()
        return {"session": self.public_session(row)}

    # -- drivers -----------------------------------------------------------

    def connection(self, harness_id):
        entry = self.entry(harness_id)
        if not entry:
            raise ValueError(f"unknown harness `{harness_id}`")
        if entry["kind"] != "acp":
            raise ValueError(f"harness `{harness_id}` is not an ACP harness")
        with self.lock:
            connection = self.connections.get(harness_id)
            if connection and connection.alive():
                return connection
            connection = AcpConnection(entry, self, allow_fallback=self.allow_adapter_install())
            connection.initialize()
            self.connections[harness_id] = connection
            return connection

    def _attach(self, row, connection):
        """Reconnect a local session to a fresh ACP process before prompting."""
        harness_session_id = row.get("harness_session_id")
        if not harness_session_id or harness_session_id in connection.sessions:
            return
        cwd = row.get("cwd") or self.host_home()
        capabilities = connection.session_capabilities()
        try:
            if capabilities.get("resume") is not None:
                result = connection.resume_session(harness_session_id, cwd)
            elif connection.capabilities.get("loadSession"):
                result = connection.load_session(harness_session_id, cwd)
            else:
                raise ValueError("this harness cannot reopen existing sessions")
        except AcpError as error:
            raise ValueError(str(error)) from error
        row["root_set"] = connection.roots(harness_session_id) or [cwd]
        self._apply_session_state(row, result)
        row["status"] = "idle"
        row.pop("error", None)
        self._save_sessions()

    def commands(self, session_id):
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        if not entry:
            return {"commands": []}
        if entry.get("kind") == "exec":
            return {"commands": []}
        if self.service_transport(entry):
            directory = row.get("cwd") or ""
            commands = opencode_service.commands(entry, self.host_home_dir, directory)
            builtins = {"compact", "model", "agent"}
            commands = [
                item for item in commands if item.get("name") not in builtins
            ]
            return {
                "commands": [
                    {
                        "name": "compact",
                        "description": "Compact the conversation context",
                        "builtin": True,
                    },
                    {
                        "name": "model",
                        "description": "Switch the session model",
                        "options": opencode_service.models(
                            entry, self.host_home_dir, directory
                        ),
                        "builtin": True,
                    },
                    {
                        "name": "agent",
                        "description": "Switch the session agent",
                        "options": opencode_service.agents(
                            entry, self.host_home_dir, directory
                        ),
                        "builtin": True,
                    },
                    *commands,
                ]
            }
        commands = []
        for item in row.get("commands") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lstrip("/")
            if not name:
                continue
            command = {
                "name": name,
                "description": str(item.get("description") or ""),
            }
            if isinstance(item.get("input"), dict):
                command["input"] = item["input"]
            commands.append(command)
        if not any(item.get("name") == "compact" for item in commands):
            commands.append(
                {
                    "name": "compact",
                    "description": "Compact the conversation context (sent to the harness)",
                    "suggested": True,
                }
            )
        return {"commands": commands}

    def command(self, session_id, name, text="", images=None):
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        if not entry:
            raise ValueError(f"harness `{row['harness']}` is no longer configured")
        name = str(name or "").strip().lstrip("/")
        if not name or not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            raise ValueError("invalid command name")
        text = (text or "").strip()
        images = images or []
        full_text = f"/{name}" + (f" {text}" if text else "")
        if entry.get("kind") == "exec":
            return self.prompt(session_id, full_text, images)
        if not self.service_transport(entry):
            return self.prompt(session_id, full_text, images)
        harness_session_id = row.get("harness_session_id") or ""
        if not harness_session_id:
            raise ValueError("this session has no harness session id")
        if name.lower() == "model":
            value = text.strip()
            if not value:
                raise ValueError("choose a model: /model provider/model")
            provider_id, _, model_id = value.partition("/")
            variant = None
            if "#" in model_id:
                model_id, variant = model_id.split("#", 1)
            if not provider_id or not model_id:
                raise ValueError("model must be provider/model")
            try:
                opencode_service.set_model(
                    entry,
                    self.host_home_dir,
                    harness_session_id,
                    provider_id,
                    model_id,
                    variant or None,
                )
            except Exception as error:  # noqa: BLE001 - HTTP boundary
                raise ValueError(str(error)) from error
            return {"session": self.public_session(row)}
        if name.lower() == "agent":
            value = text.strip()
            if not value:
                raise ValueError("choose an agent: /agent name")
            try:
                opencode_service.set_agent(
                    entry, self.host_home_dir, harness_session_id, value
                )
            except Exception as error:  # noqa: BLE001 - HTTP boundary
                raise ValueError(str(error)) from error
            return {"session": self.public_session(row)}
        if name.lower() == "compact":
            reply = opencode_service.compact(entry, self.host_home_dir, harness_session_id)
            message_id = str(((reply or {}).get("data") or {}).get("id") or "")
            if self._service_claim(row["id"], message_id):
                self._emit(
                    row["id"],
                    {
                        "type": "message",
                        "role": "user",
                        "message_id": message_id or "user-" + uuid.uuid4().hex[:10],
                        "content": {"type": "text", "text": "/compact"},
                    },
                )
            return {"session": self.public_session(row)}
        known = {item.get("name") for item in self.commands(session_id)["commands"]}
        if name in known:
            try:
                opencode_service.command(
                    entry,
                    self.host_home_dir,
                    harness_session_id,
                    name,
                    text,
                    images,
                )
            except Exception as error:  # noqa: BLE001 - HTTP boundary
                raise ValueError(str(error)) from error
            return {"session": self.public_session(row)}
        return self._prompt_service(row, entry, full_text, images)

    def prompt(self, session_id, text=None, images=None):
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        if not entry:
            raise ValueError(f"harness `{row['harness']}` is no longer configured")
        text = (text or "").strip()
        images = images or []
        if not text and not images:
            raise ValueError("a prompt or image is required")
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for image in images:
            if not isinstance(image, dict) or not image.get("data"):
                continue
            blocks.append(
                {
                    "type": "image",
                    "data": image["data"],
                    "mimeType": image.get("mimeType") or "image/png",
                }
            )
        if not self.service_transport(entry):
            self._emit(
                session_id,
                {
                    "type": "message",
                    "role": "user",
                    "message_id": "user-" + uuid.uuid4().hex[:10],
                    "content": {"type": "text", "text": text},
                },
            )
        if entry["kind"] == "exec":
            return self._run_exec(row, entry, text)
        if self.service_transport(entry):
            return self._prompt_service(row, entry, text, images)
        connection = self.connection(row["harness"])
        if not row.get("harness_session_id"):
            raise ValueError("this session has no harness session id")
        self._attach(row, connection)
        row["root_set"] = connection.roots(row["harness_session_id"]) or row.get("root_set") or [row["cwd"]]
        self.set_status(session_id, "working")

        def run():
            try:
                result = connection.prompt(row["harness_session_id"], blocks)
                self._emit(session_id, {"type": "turn_end", "stop_reason": (result or {}).get("stopReason") or "end_turn"})
                self.set_status(session_id, "idle")
            except AcpError as error:
                self._emit(session_id, {"type": "error", "error": str(error)})
                self.set_status(session_id, "failed")
            except Exception as error:  # noqa: BLE001 - background boundary
                self._emit(session_id, {"type": "error", "error": str(error)})
                self.set_status(session_id, "failed")

        threading.Thread(target=run, name=f"prompt-{session_id}", daemon=True).start()
        return {"session": self.public_session(row)}

    def _run_exec(self, row, entry, text):
        driver = ExecDriver(self, entry)
        self.exec_drivers[row["id"]] = driver
        self.set_status(row["id"], "working")

        def run():
            try:
                process = driver.start(text, row["cwd"])
                session_seen = row.get("harness_session_id") or ""
                for line in process.stdout:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    event = {"type": "message", "role": "agent", "content": {"type": "text", "text": line}}
                    if line.lstrip().startswith("{"):
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            payload = None
                        if isinstance(payload, dict):
                            for key in PROMPT_SESSION_KEYS:
                                value = payload.get(key)
                                if isinstance(value, str) and value.strip() and not session_seen:
                                    session_seen = value.strip()
                                    row["harness_session_id"] = session_seen
                    self._emit(row["id"], event)
                code = process.wait()
                if not row.get("harness_session_id") and session_seen:
                    row["harness_session_id"] = session_seen
                self._emit(
                    row["id"],
                    {"type": "turn_end", "stop_reason": "end_turn" if code == 0 else f"exit {code}"},
                )
                self.set_status(row["id"], "idle" if code == 0 else "failed")
                self._save_sessions()
            except Exception as error:  # noqa: BLE001 - background boundary
                self._emit(row["id"], {"type": "error", "error": str(error)})
                self.set_status(row["id"], "failed")
            finally:
                self.exec_drivers.pop(row["id"], None)

        threading.Thread(target=run, name=f"exec-{row['id']}", daemon=True).start()
        return {"session": self.public_session(row)}

    def cancel(self, session_id):
        row = self.get(session_id)
        driver = self.exec_drivers.get(session_id)
        if driver:
            driver.cancel()
            return {"cancelled": True}
        entry = self.entry(row["harness"])
        if self.service_transport(entry):
            session = row.get("harness_session_id") or ""
            if session:
                try:
                    opencode_service.interrupt(entry, self.host_home_dir, session)
                except Exception as error:  # noqa: BLE001 - HTTP boundary
                    raise ValueError(str(error)) from error
            return {"cancelled": True}
        connection = self.connections.get(row["harness"])
        if connection and row.get("harness_session_id"):
            connection.cancel(row["harness_session_id"])
            for request_id, pending in list(self.pending_permissions.items()):
                if pending.get("session_id") == session_id:
                    self.resolve_permission(request_id, None)
            return {"cancelled": True}
        return {"cancelled": False}

    def resume(self, session_id):
        row = self.get(session_id)
        entry = self.entry(row["harness"])
        if not entry or entry["kind"] != "acp" or not row.get("harness_session_id"):
            raise ValueError("this session cannot be resumed")
        if self.service_transport(entry):
            row.pop("error", None)
            if row.get("status") != "idle":
                self.set_status(session_id, "idle")
            return {"session": self.public_session(row)}
        connection = self.connection(row["harness"])
        attached = row["harness_session_id"] in connection.sessions
        if row.get("status") != "detached" and attached:
            return {"session": self.public_session(row)}
        self._attach(row, connection)
        row.pop("error", None)
        self.set_status(session_id, "idle")
        self._save_sessions()
        return {"session": self.public_session(row)}

    def config(self, session_id, config_id, value, boolean=False):
        row = self.get(session_id)
        if self.service_transport(self.entry(row["harness"])):
            return {"config_options": row.get("config_options") or []}
        connection = self.connections.get(row["harness"])
        if not connection or not row.get("harness_session_id"):
            raise ValueError("this session is not connected")
        result = connection.set_config(row["harness_session_id"], config_id, value, boolean)
        if isinstance(result, dict) and result.get("configOptions"):
            row["config_options"] = result["configOptions"]
            self._emit(session_id, {"type": "config", "config_options": result["configOptions"]})
        return {"config_options": row.get("config_options") or []}

    # -- ACP host ----------------------------------------------------------

    def host_home(self):
        return self.host_home_dir

    def log(self, line):
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{now()} {line}\n")
        except OSError:
            pass

    def roots_for(self, session_id):
        connection = None
        row = self.find_by_harness_session(session_id)
        if not row:
            return []
        connection = self.connections.get(row["harness"])
        if not connection:
            return []
        return connection.roots(session_id) or [row.get("cwd") or ""]

    def permission(self, harness_session_id, tool_call, options):
        row = self.find_by_harness_session(harness_session_id)
        if not row:
            return {"outcome": "cancelled"}
        request_id = "perm_" + uuid.uuid4().hex[:12]
        event = threading.Event()
        pending = {
            "session_id": row["id"],
            "event": event,
            "decision": None,
            "options": options,
        }
        with self.lock:
            self.pending_permissions[request_id] = pending
        self._emit(
            row["id"],
            {
                "type": "permission_request",
                "request_id": request_id,
                "tool_call": {
                    "call_id": tool_call.get("toolCallId") or "",
                    "title": tool_call.get("title"),
                    "kind": tool_call.get("kind"),
                    "content": [self._safe_tool_content(item) for item in tool_call.get("content") or []],
                    "locations": tool_call.get("locations") or [],
                    "raw_input": self._cap(tool_call.get("rawInput")),
                },
                "options": [
                    {
                        "option_id": option.get("optionId"),
                        "name": option.get("name"),
                        "kind": option.get("kind"),
                    }
                    for option in options
                    if isinstance(option, dict)
                ],
            },
        )
        self.set_status(row["id"], "waiting")
        event.wait(60 * 60)
        with self.lock:
            self.pending_permissions.pop(request_id, None)
        decision = pending.get("decision")
        self._emit(
            row["id"],
            {
                "type": "permission_resolved",
                "request_id": request_id,
                "option_id": decision,
            },
        )
        if row.get("status") == "waiting":
            self.set_status(row["id"], "working")
        if decision:
            return {"outcome": "selected", "optionId": decision}
        return {"outcome": "cancelled"}

    def resolve_permission(self, request_id, option_id):
        with self.lock:
            pending = self.pending_permissions.get(request_id)
        if not pending:
            raise KeyError(request_id)
        if pending.get("native"):
            row = self.sessions.get(pending.get("session_id"))
            entry = self.entry(row["harness"]) if row else None
            if not entry:
                raise ValueError("this session is no longer configured")
            mapping = {
                "allow_once": "once",
                "allow_always": "always",
                "reject_once": "reject",
                "once": "once",
                "always": "always",
                "reject": "reject",
            }
            reply = mapping.get(str(option_id))
            if not reply:
                raise ValueError("unknown permission option")
            try:
                opencode_service.reply_permission(
                    entry,
                    self.host_home_dir,
                    pending.get("harness_session_id") or "",
                    request_id,
                    reply,
                )
            except Exception as error:  # noqa: BLE001 - HTTP boundary
                raise ValueError(str(error)) from error
            with self.lock:
                self.pending_permissions.pop(request_id, None)
            self._emit(
                pending["session_id"],
                {
                    "type": "permission_resolved",
                    "request_id": request_id,
                    "option_id": option_id,
                },
            )
            if row and row.get("status") == "waiting":
                self.set_status(pending["session_id"], "working")
            return {"request_id": request_id}
        pending["decision"] = option_id
        pending["event"].set()
        return {"request_id": request_id}

    def read_text(self, path, line=None, limit=None):
        path = os.path.abspath(os.path.expanduser(path))
        roots = self._roots_for_path_lookup(path)
        if roots is None or not any(_within(root, path) for root in roots):
            raise AcpError("file is outside the session workspace")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as error:
            raise AcpError(f"could not read {path}: {error}") from error
        if line is not None:
            try:
                start = max(0, int(line) - 1)
                rows = text.splitlines(keepends=True)
                text = "".join(rows[start:])
            except (TypeError, ValueError):
                pass
        if limit is not None:
            try:
                text = text[: max(0, int(limit))]
            except (TypeError, ValueError):
                pass
        self.audit("fs.read", path)
        return text

    def write_text(self, path, content):
        path = os.path.abspath(os.path.expanduser(path))
        roots = self._roots_for_path_lookup(path)
        if roots is None:
            raise AcpError("no active session for this file")
        if not any(_within(root, path) for root in roots):
            raise AcpError("refusing to write outside the session workspace")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as error:
            raise AcpError(f"could not write {path}: {error}") from error
        self.audit("fs.write", path, f"{len(content)} bytes")

    def _roots_for_path_lookup(self, path):
        # Find an active session whose root set contains this path. When there
        # is exactly one live session, use it so a fresh session can write its
        # first file before the UI records the root set.
        live = []
        for row in self.sessions.values():
            if row.get("status") in {"working", "waiting", "idle"} and row.get("root_set"):
                live.append(row)
        for row in live:
            if any(_within(root, path) for root in row["root_set"]):
                return row["root_set"]
        return live[0]["root_set"] if len(live) == 1 else None

    def create_terminal(self, harness_session_id, command, args, env_pairs, cwd, output_limit):
        row = self.find_by_harness_session(harness_session_id)
        if not row:
            raise AcpError("unknown session")
        connection = self.connections.get(row["harness"])
        if not connection or not connection.entry.get("delegate_terminal"):
            raise AcpError("terminal delegation is disabled for this harness")
        cwd = self.normalize_cwd(cwd or row.get("cwd") or "")
        roots = connection.roots(harness_session_id) or row.get("root_set") or []
        if not any(_within(root, cwd) for root in roots):
            raise AcpError("terminal cwd is outside the session workspace")
        if not command:
            raise AcpError("terminal command is required")
        binary = command if os.path.isabs(command) else shutil.which(command)
        if not binary:
            raise AcpError(f"`{command}` was not found on PATH")
        env = sanitized_env(self.host_home_dir)
        for item in env_pairs or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name or "=" in name or name.startswith("ORQELLA_"):
                continue
            env[name] = str(item.get("value") or "")
        terminal = Terminal(binary, [str(arg) for arg in args or []], cwd, env, output_limit)
        terminal.session_id = harness_session_id
        with self.lock:
            self.terminals[terminal.id] = terminal
        self.audit("terminal", cwd, command + " " + " ".join(str(arg) for arg in args or []))
        return terminal

    def terminal_output(self, terminal_id):
        terminal = self.terminals.get(terminal_id)
        if not terminal:
            raise KeyError(terminal_id)
        return terminal.output()

    def close_terminals(self, harness_session_id):
        for terminal_id, terminal in list(self.terminals.items()):
            if getattr(terminal, "session_id", "") == harness_session_id:
                terminal.release()
                self.terminals.pop(terminal_id, None)

    def normalize_cwd(self, cwd):
        value = str(cwd or "").strip()
        if not value:
            value = self.host_home_dir
        value = os.path.abspath(os.path.expanduser(value))
        if not os.path.isdir(value):
            raise ValueError(f"`{value}` is not a directory")
        return value

    def audit(self, action, path, detail=""):
        try:
            self.store.append_jsonl(
                "audit.jsonl",
                {"at": now(), "action": action, "path": path, "detail": detail[:400]},
            )
        except OSError:
            pass

    def close(self):
        for connection in list(self.connections.values()):
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - shutdown boundary
                pass
        self.connections.clear()
