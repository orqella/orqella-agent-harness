"""Agent Client Protocol client (ACP v1, JSON-RPC 2.0 over stdio).

The module is the client: it spawns the harness, multiplexes sessions, streams
normalized updates to the UI, and mediates filesystem and terminal requests.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT_LIMIT = 1024 * 1024
MAX_OUTPUT_LIMIT = 4 * 1024 * 1024
TERMINAL_TIMEOUT_SECONDS = 30 * 60


class AcpError(RuntimeError):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class AcpUnavailable(AcpError):
    """The agent binary or adapter could not be started."""


class AcpProtocolError(AcpError):
    """The agent returned malformed or unexpected protocol data."""


def _within(root, path):
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_root, real_path]) == real_root
    except (OSError, ValueError):
        return False


def strip_control(text):
    """Remove terminal control sequences for display-safe event payloads."""
    import re

    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(
        character
        for character in text
        if character in "\n\t\r" or ord(character) >= 32
    )


class Terminal:
    def __init__(self, command, args, cwd, env, output_limit):
        self.id = "term_" + uuid.uuid4().hex[:16]
        self.command = command
        self.args = args
        self.cwd = cwd
        self.output_limit = max(4096, min(int(output_limit or DEFAULT_OUTPUT_LIMIT), MAX_OUTPUT_LIMIT))
        self.buffer = b""
        self.truncated = False
        self.exit_status = None
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.process = subprocess.Popen(
            [command, *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, name=f"acp-terminal-{self.id}", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                chunk = self.process.stdout.read(4096)
                if not chunk:
                    break
                with self.lock:
                    self.buffer += chunk
                    if len(self.buffer) > self.output_limit:
                        overflow = len(self.buffer) - self.output_limit
                        self.buffer = self.buffer[overflow:]
                        self.truncated = True
        except (OSError, ValueError):
            pass
        finally:
            code = self.process.wait()
            with self.lock:
                self.exit_status = {
                    "exitCode": code,
                    "signal": signal.Signals(-code).name if code is not None and code < 0 else None,
                }
            self.done.set()

    def output(self):
        with self.lock:
            return {
                "output": self.buffer.decode("utf-8", errors="replace"),
                "truncated": self.truncated,
                **({"exitStatus": self.exit_status} if self.exit_status else {}),
            }

    def kill(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    self.process.terminate()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass

    def release(self):
        self.kill()
        try:
            self.process.stdout.close()
        except (OSError, ValueError):
            pass


class AcpConnection:
    """One agent process with multiplexed ACP sessions."""

    def __init__(self, entry, host, allow_fallback=False):
        self.entry = entry
        self.host = host
        self.allow_fallback = allow_fallback
        self.process = None
        self.stdin = None
        self.stdout = None
        self.capabilities = {}
        self.agent_info = {}
        self.sessions = {}
        self.terminals = {}
        self._next_id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._stderr_tail = ""
        self.last_error = ""
        self.started_at = 0.0

    # -- process lifecycle -------------------------------------------------

    def start(self):
        if self.process and self.process.poll() is None:
            return
        from registry import resolve_command

        command, args, source = resolve_command(self.entry, allow_fallback=self.allow_fallback)
        if not command:
            raise AcpUnavailable(source or f"`{self.entry.get('command')}` is not installed")
        env = sanitized_env(self.host.host_home())
        for key, value in (self.entry.get("env") or {}).items():
            resolved = resolve_env_value(value)
            if resolved is not None:
                env[key] = resolved
        try:
            self.process = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.host.host_home(),
                env=env,
            )
        except OSError as error:
            raise AcpUnavailable(f"could not start `{command}`: {error}") from error
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.started_at = time.monotonic()
        self._closed.clear()
        threading.Thread(target=self._read_stdout, name="acp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="acp-stderr", daemon=True).start()
        self.host.log(f"started {self.entry.get('id')} via {command} ({source})")

    def alive(self):
        return bool(self.process and self.process.poll() is None)

    def close(self):
        self._closed.set()
        for terminal in list(self.terminals.values()):
            terminal.release()
        self.terminals.clear()
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        self.process = None
        self._close_pipes()
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item["error"] = AcpError("agent process exited")
            item["event"].set()

    def _close_pipes(self):
        for stream in (self.stdin, self.stdout):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            if self.process and self.process.stderr:
                self.process.stderr.close()
        except (OSError, ValueError):
            pass
        self.stdin = None
        self.stdout = None

    def _fail(self, message):
        if self._closed.is_set():
            return
        self.last_error = message
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item["error"] = AcpError(message)
            item["event"].set()
        for session_id in list(self.sessions):
            self.host.detached(session_id, message)

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                line = line.rstrip("\n")
                if line:
                    self._stderr_tail = (self._stderr_tail + line + "\n")[-4000:]
                    self.host.log(line)
        except (OSError, ValueError):
            pass

    def _read_stdout(self):
        try:
            for line in self.stdout:
                line = line.strip()
                if not line:
                    continue
                if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                    self.host.log("discarded an oversized ACP message")
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.host.log(f"ignored non-JSON ACP output: {line[:200]}")
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            code = None
            try:
                code = self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
            detail = self._stderr_tail.strip().splitlines()[-1] if self._stderr_tail.strip() else ""
            reason = f"agent exited with code {code}" if code is not None else "agent stdout closed"
            if detail:
                reason += f": {detail}"
            self._close_pipes()
            self._fail(reason)

    # -- JSON-RPC ----------------------------------------------------------

    def _write(self, payload):
        if not self.stdin or self.process.poll() is not None:
            raise AcpError("agent process is not running")
        data = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                self.stdin.write(data)
                self.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                raise AcpError(f"could not write to the agent: {error}") from error

    def request(self, method, params, timeout=60):
        if not self.alive():
            raise AcpError("agent process is not running")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            item = {"event": threading.Event(), "result": None, "error": None}
            self._pending[request_id] = item
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not item["event"].wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise AcpError(f"`{method}` timed out")
        if item["error"]:
            raise item["error"]
        return item["result"]

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _dispatch(self, message):
        if "id" in message and ("result" in message or "error" in message):
            with self._lock:
                item = self._pending.pop(message.get("id"), None)
            if not item:
                return
            error = message.get("error")
            if error:
                item["error"] = AcpError(
                    str(error.get("message") or "agent error"),
                    error.get("code"),
                    error.get("data"),
                )
            else:
                item["result"] = message.get("result")
            item["event"].set()
            return
        method = message.get("method")
        if not method:
            return
        params = message.get("params") or {}
        if "id" in message:
            # Agent → client request. Handle out of band so a permission wait
            # or a long command never blocks the reader.
            request_id = message["id"]
            threading.Thread(
                target=self._handle_client_request,
                args=(request_id, method, params),
                name=f"acp-client-{method}",
                daemon=True,
            ).start()
            return
        if method == "session/update":
            session_id = params.get("sessionId") or ""
            update = params.get("update") or {}
            self.host.update(session_id, update)
        elif method in {"session/request_permission", "fs/read_text_file", "fs/write_text_file"}:
            self.host.log(f"unexpected notification {method}")

    def _handle_client_request(self, request_id, method, params):
        try:
            result = self.handle_request(method, params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except AcpError as error:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(error)},
            }
        except Exception as error:  # noqa: BLE001 - protocol boundary
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": str(error)},
            }
        try:
            self._write(response)
        except AcpError:
            pass

    # -- client-side methods ----------------------------------------------

    def handle_request(self, method, params):
        if method == "session/request_permission":
            session_id = params.get("sessionId") or ""
            tool_call = params.get("toolCall") or {}
            options = params.get("options") or []
            return {"outcome": self.host.permission(session_id, tool_call, options)}
        if method == "fs/read_text_file":
            return {"content": self.host.read_text(params.get("path") or "", params.get("line"), params.get("limit"))}
        if method == "fs/write_text_file":
            self.host.write_text(params.get("path") or "", params.get("content") or "")
            return {}
        if method.startswith("terminal/"):
            return self._terminal(method, params)
        raise AcpError(f"unsupported client method `{method}`")

    def _terminal(self, method, params):
        session_id = params.get("sessionId") or ""
        if method == "terminal/create":
            terminal = self.host.create_terminal(
                session_id,
                params.get("command") or "",
                params.get("args") or [],
                params.get("env") or [],
                params.get("cwd"),
                params.get("outputByteLimit"),
            )
            self.terminals[terminal.id] = terminal
            return {"terminalId": terminal.id}
        terminal_id = params.get("terminalId") or ""
        terminal = self.terminals.get(terminal_id)
        if not terminal:
            raise AcpError(f"unknown terminal `{terminal_id}`")
        if method == "terminal/output":
            return terminal.output()
        if method == "terminal/wait_for_exit":
            if not terminal.done.wait(TERMINAL_TIMEOUT_SECONDS):
                raise AcpError("terminal wait timed out")
            return terminal.exit_status or {"exitCode": None, "signal": None}
        if method == "terminal/kill":
            terminal.kill()
            return {}
        if method == "terminal/release":
            terminal.release()
            self.terminals.pop(terminal_id, None)
            return {}
        raise AcpError(f"unknown terminal method `{method}`")

    # -- ACP session lifecycle --------------------------------------------

    def initialize(self):
        self.start()
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": self.client_capabilities(),
                "clientInfo": {"name": "Orqella Agent Harness", "version": "0.1.0"},
            },
            timeout=90,
        )
        if not isinstance(result, dict):
            raise AcpProtocolError("initialize returned no capabilities")
        version = result.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise AcpProtocolError(
                f"agent speaks ACP version {version}; this module requires {PROTOCOL_VERSION}"
            )
        self.capabilities = result.get("agentCapabilities") or {}
        self.agent_info = result.get("agentInfo") or {}
        return result

    def client_capabilities(self):
        capabilities = {"session": {"configOptions": {"boolean": {}}}}
        if self.entry.get("delegate_fs"):
            capabilities["fs"] = {"readTextFile": True, "writeTextFile": True}
        if self.entry.get("delegate_terminal"):
            capabilities["terminal"] = True
        return capabilities

    def session_capabilities(self):
        return self.capabilities.get("sessionCapabilities") or {}

    def mcp_servers(self):
        provider = getattr(self.host, "mcp_servers", None)
        if not callable(provider):
            return []
        try:
            servers = provider()
        except Exception:
            return []
        return servers if isinstance(servers, list) else []

    def new_session(self, cwd, additional_directories=None):
        params = {"cwd": cwd, "mcpServers": self.mcp_servers()}
        if additional_directories and self.session_capabilities().get("additionalDirectories") is not None:
            params["additionalDirectories"] = additional_directories
        result = self.request("session/new", params, timeout=120)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise AcpProtocolError("session/new returned no session id")
        self.sessions[result["sessionId"]] = {
            "cwd": cwd,
            "roots": [cwd, *(additional_directories or [])],
        }
        return result

    def load_session(self, session_id, cwd, additional_directories=None):
        params = {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": self.mcp_servers(),
        }
        if additional_directories and self.session_capabilities().get("additionalDirectories") is not None:
            params["additionalDirectories"] = additional_directories
        result = self.request("session/load", params, timeout=300)
        self.sessions[session_id] = {
            "cwd": cwd,
            "roots": [cwd, *(additional_directories or [])],
        }
        return result or {}

    def resume_session(self, session_id, cwd, additional_directories=None):
        params = {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": self.mcp_servers(),
        }
        if additional_directories and self.session_capabilities().get("additionalDirectories") is not None:
            params["additionalDirectories"] = additional_directories
        result = self.request("session/resume", params, timeout=120)
        self.sessions[session_id] = {
            "cwd": cwd,
            "roots": [cwd, *(additional_directories or [])],
        }
        return result or {}

    def list_sessions(self, cwd=None, cursor=None):
        if self.session_capabilities().get("list") is None:
            return None
        params = {}
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        return self.request("session/list", params, timeout=30) or {}

    def prompt(self, session_id, blocks):
        return self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": blocks},
            timeout=None,
        )

    def cancel(self, session_id):
        self.notify("session/cancel", {"sessionId": session_id})

    def set_config(self, session_id, config_id, value, boolean=False):
        params = {"sessionId": session_id, "configId": config_id, "value": value}
        if boolean:
            params["type"] = "boolean"
        return self.request("session/set_config_option", params, timeout=30)

    def set_mode(self, session_id, mode_id):
        return self.request("session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout=30)

    def delete_session(self, session_id):
        if self.session_capabilities().get("delete") is None:
            return False
        try:
            self.request("session/delete", {"sessionId": session_id}, timeout=30)
        except AcpError:
            return False
        self.host.close_terminals(session_id)
        self.sessions.pop(session_id, None)
        return True

    def close_session(self, session_id):
        if self.session_capabilities().get("close") is not None:
            try:
                self.request("session/close", {"sessionId": session_id}, timeout=15)
            except AcpError:
                pass
        self.host.close_terminals(session_id)
        self.sessions.pop(session_id, None)

    def roots(self, session_id):
        value = self.sessions.get(session_id) or {}
        return list(value.get("roots") or [])

    def session_cwd(self, session_id):
        value = self.sessions.get(session_id) or {}
        return value.get("cwd") or self.host.host_home()


def resolve_env_value(value):
    value = str(value or "").strip()
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1]) or None
    if value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:]) or None
    return value


def sanitized_env(host_home):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ORQELLA_"):
            env.pop(key, None)
    env["HOME"] = host_home
    return env
