"""Model Context Protocol servers, as tools the chat model can reach.

Redtram is an MCP *client*. A server is named in a JSON file -- the same shape
Claude Desktop and Cursor use, so an existing config can be pointed at
directly::

    {
      "mcpServers": {
        "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        "search": {"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer …"}}
      }
    }

Two transports are spoken, both over plain JSON-RPC 2.0:

* ``command`` launches a child process and talks newline-delimited JSON over its
  stdin/stdout (the stdio transport);
* ``url`` POSTs to the endpoint and accepts either a JSON body or an SSE stream
  (the streamable-HTTP transport), capturing the session id the server hands
  back on ``initialize``.

Only ``tools`` are used: on start each server is asked for ``tools/list`` and
every tool is offered to the model as ``mcp__<server>__<tool>`` -- a namespace
that cannot collide with the built-in file tools or with a second server that
happens to use the same tool name. ``tools/call`` results are flattened to text
so they land in the transcript the same way every other tool's output does.

Nothing here is allowed to be fatal. A server that will not start, hangs,
speaks nonsense, or dies mid-session is recorded with the reason and the rest of
the session carries on without its tools; that is why every call in this module
returns a status rather than raising into the caller.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import ToolResult

#: What this client claims to speak. Servers that know a newer revision answer
#: with theirs, which we accept -- the tool methods used here are stable across
#: every revision so far.
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "k0b0l", "version": "1.0"}

#: One server must not be able to wedge the UI at startup, so bootstrapping has
#: a budget separate from (and shorter than) the per-call timeout.
START_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0
#: Ceiling on what one MCP tool result contributes to the transcript.
MAX_RESULT_CHARS = 24_000
#: How much server stderr is kept for the ``/mcp`` diagnostics.
STDERR_TAIL = 4_000

#: ``mcp__<server>__<tool>`` has to survive both Ollama's and OpenAI's function
#: name rules (letters, digits, ``_``, ``-``), so anything else is folded to ``_``.
_NAME_OK = re.compile(r"[^A-Za-z0-9_-]")
MAX_NAME = 64


class MCPError(RuntimeError):
    """A server failed to start, to answer, or answered with an error."""


def _clean(part: str) -> str:
    return _NAME_OK.sub("_", part).strip("_") or "unnamed"


@dataclass
class ToolSpec:
    """One server tool, under the name the model sees."""

    server: str
    name: str
    exposed: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    def openai_schema(self) -> dict[str, Any]:
        parameters = self.schema if isinstance(self.schema, dict) and self.schema else {
            "type": "object",
            "properties": {},
        }
        return {
            "type": "function",
            "function": {
                "name": self.exposed,
                "description": (self.description or f"{self.name} on the {self.server} MCP server")[:1024],
                "parameters": parameters,
            },
        }


@dataclass
class ServerSpec:
    """One entry from the config file, before anything is launched."""

    name: str
    command: tuple[str, ...] = ()
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def describe(self) -> str:
        if self.url:
            return self.url
        return " ".join(self.command) if self.command else "(no command)"


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------
class _Stdio:
    """JSON-RPC over a child process's stdin/stdout, one message per line."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.process: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition()
        self._answers: dict[int, dict[str, Any]] = {}
        self._stderr: queue.Queue[str] = queue.Queue()
        self._stderr_seen: list[str] = []
        self._closed = False

    def start(self) -> None:
        argv = list(self.spec.command)
        if not argv:
            raise MCPError("no command to run")
        program = shutil.which(argv[0]) or argv[0]
        if not shutil.which(argv[0]) and not Path(argv[0]).exists():
            raise MCPError(f"{argv[0]!r} is not installed or not on PATH")
        env = {**os.environ}
        # The child's own view of its environment; servers commonly need a token.
        env.update({str(key): str(value) for key, value in self.spec.env.items()})
        try:
            self.process = subprocess.Popen(  # noqa: S603 - a configured argv, no shell
                [program, *argv[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.spec.cwd or None,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise MCPError(f"could not start {argv[0]!r}: {exc}") from exc
        threading.Thread(target=self._read_stdout, name=f"mcp-{self.spec.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.spec.name}-err", daemon=True).start()

    # -- reader threads ---------------------------------------------------
    def _read_stdout(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # Servers are allowed to print banners; ignore what is not JSON.
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._condition:
                    self._answers[int(message["id"])] = message
                    self._condition.notify_all()
            elif "method" in message:
                self._answer_server(message)
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _read_stderr(self) -> None:
        stream = self.process.stderr if self.process else None
        if stream is None:
            return
        for raw in stream:
            text = raw.decode("utf-8", "replace").rstrip("\n")
            if text:
                self._stderr.put(text)

    def _answer_server(self, message: dict[str, Any]) -> None:
        """Reply to the few server->client requests that have an obvious answer.

        ``roots/list`` gets an empty list (this client offers no roots) and
        ``ping`` gets an empty result; anything else is answered with a
        "method not found" so a server never waits on us forever.
        """
        method = message.get("method")
        if "id" not in message:
            return  # a notification; nothing to answer
        if method == "roots/list":
            result: Any = {"roots": []}
        elif method in ("ping", "notifications/ping"):
            result = {}
        else:
            self._send(
                {"jsonrpc": "2.0", "id": message["id"],
                 "error": {"code": -32601, "message": f"{method} is not supported by K0B0L"}}
            )
            return
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    # -- protocol ---------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise MCPError("the server is not running")
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"the server closed its input ({exc})") from exc

    def request(self, method: str, params: dict[str, Any], timeout: float = CALL_TIMEOUT) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._answers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"{method} timed out after {timeout:.0f}s{self._why()}")
                if self._closed or self._died():
                    raise MCPError(f"the server exited while running {method}{self._why()}")
                self._condition.wait(min(remaining, 0.25))
            message = self._answers.pop(request_id)
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(
                f"{method} failed: {error.get('message') or error} (code {error.get('code')})"
            )
        return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except MCPError:
            pass  # a notification nobody hears is not worth failing the start

    def _died(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def _why(self) -> str:
        for _ in range(200):
            try:
                self._stderr_seen.append(self._stderr.get_nowait())
            except queue.Empty:
                break
        tail = [line for line in self._stderr_seen if line.strip()][-6:]
        while len(self._stderr_seen) > 200:
            self._stderr_seen.pop(0)
        return f"; server said: {' | '.join(tail)}" if tail else ""

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError:
            pass

    def describe(self) -> str:
        return self.spec.describe()


class _Http:
    """JSON-RPC over streamable HTTP, JSON or SSE in reply."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.session_id = ""
        self._next_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.spec.url.startswith(("http://", "https://")):
            raise MCPError(f"{self.spec.url!r} is not an http(s) url")

    # -- protocol ---------------------------------------------------------
    def _post(self, payload: dict[str, Any], timeout: float) -> tuple[str, str, str]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **{str(k): str(v) for k, v in (self.spec.env or {}).items()},
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.spec.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured url
                session = response.headers.get("Mcp-Session-Id") or ""
                content_type = response.headers.get("Content-Type", "")
                return response.read().decode("utf-8", "replace"), content_type, session
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise MCPError(f"HTTP {exc.code} from {self.spec.url}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"could not reach {self.spec.url}: {exc}") from exc

    def request(self, method: str, params: dict[str, Any], timeout: float = CALL_TIMEOUT) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        text, content_type, session = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, timeout
        )
        if session and not self.session_id:
            self.session_id = session
        messages: list[dict[str, Any]] = []
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        decoded = json.loads(chunk)
                    except ValueError:
                        continue
                    if isinstance(decoded, dict):
                        messages.append(decoded)
        elif text.strip():
            try:
                decoded = json.loads(text)
            except ValueError as exc:
                raise MCPError(f"{self.spec.url} answered with something that is not JSON: {exc}") from exc
            messages = decoded if isinstance(decoded, list) else [decoded]
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"] or {}
                raise MCPError(
                    f"{method} failed: {error.get('message') or error} (code {error.get('code')})"
                )
            return message.get("result")
        raise MCPError(f"{self.spec.url} never answered {method}")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params}, CALL_TIMEOUT)
        except MCPError:
            pass

    def close(self) -> None:
        return

    def describe(self) -> str:
        return self.spec.url


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------
class MCPServer:
    """One MCP server: a process or an endpoint, plus the tools it offers."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.transport: _Stdio | _Http | None = None
        self.state = "idle"
        self.error = ""
        self.tools: list[ToolSpec] = []
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.instructions = ""

    # -- lifecycle --------------------------------------------------------
    def start(self, timeout: float = START_TIMEOUT) -> None:
        if self.state == "ready":
            return
        self.error = ""
        try:
            transport: _Stdio | _Http = _Http(self.spec) if self.spec.url else _Stdio(self.spec)
            transport.start()
            self.transport = transport
            result = transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": CLIENT_INFO,
                },
                timeout,
            )
            if not isinstance(result, dict):
                raise MCPError("initialize answered with no server description")
            self.protocol_version = str(result.get("protocolVersion") or "")
            self.server_info = result.get("serverInfo") or {}
            self.instructions = str(result.get("instructions") or "")
            transport.notify("notifications/initialized", {})
            self.tools = self._list_tools(timeout)
            self.state = "ready"
        except Exception as exc:  # noqa: BLE001 - one bad server must not stop the session
            self.error = str(exc)
            self.state = "failed"
            self.close(keep_error=True)

    def _list_tools(self, timeout: float) -> list[ToolSpec]:
        tools: list[ToolSpec] = []
        cursor = ""
        seen = 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            assert self.transport is not None
            result = self.transport.request("tools/list", params, timeout)
            if not isinstance(result, dict):
                break
            for entry in result.get("tools") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                tools.append(self._spec_for(str(entry["name"]), entry))
            cursor = str(result.get("nextCursor") or "")
            seen += 1
            if not cursor or seen > 20:  # a server looping on cursors must not spin forever
                break
        return tools

    def _spec_for(self, name: str, entry: dict[str, Any]) -> ToolSpec:
        exposed = f"mcp__{_clean(self.spec.name)}__{_clean(name)}"[:MAX_NAME]
        schema = entry.get("inputSchema")
        return ToolSpec(
            server=self.spec.name,
            name=name,
            exposed=exposed,
            description=str(entry.get("description") or ""),
            schema=schema if isinstance(schema, dict) else {},
        )

    def call(self, tool_name: str, arguments: dict[str, Any], timeout: float = CALL_TIMEOUT) -> ToolResult:
        """Run one tool on this server and flatten the result to text."""
        if self.state != "ready" or self.transport is None:
            return ToolResult(False, f"MCP server {self.spec.name!r} is not connected ({self.error or self.state})")
        started = time.monotonic()
        try:
            result = self.transport.request(
                "tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout
            )
        except MCPError as exc:
            return ToolResult(False, f"mcp__{self.spec.name}__{tool_name}: {exc}")
        elapsed = time.monotonic() - started
        if not isinstance(result, dict):
            return ToolResult(False, f"mcp__{self.spec.name}__{tool_name}: empty result")
        text = _flatten(result.get("content"))
        if result.get("structuredContent") is not None and not text:
            text = json.dumps(result["structuredContent"], indent=2, ensure_ascii=False)
        header = f"mcp__{self.spec.name}__{tool_name} -> {elapsed:.2f}s"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n… truncated at {MAX_RESULT_CHARS} characters"
        return ToolResult(
            ok=not result.get("isError"),
            text=f"{header}\n{text}" if text else f"{header}\n(no content)",
        )

    def close(self, *, keep_error: bool = False) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.tools = []
        if not keep_error:
            self.state = "stopped" if self.state == "ready" else self.state

    def status(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "transport": self.spec.transport,
            "target": self.spec.describe(),
            "state": self.state,
            "tools": len(self.tools),
            "error": self.error,
            "server": str((self.server_info or {}).get("name") or ""),
            "version": str((self.server_info or {}).get("version") or ""),
        }

    def lines(self) -> list[str]:
        """Human-readable status, one line per fact."""
        mark = {"ready": "ok", "failed": "FAILED", "idle": "-", "stopped": "stopped"}.get(self.state, self.state)
        out = [f"{mark:>7}  {self.name_line()}"]
        if self.server_info:
            out.append(
                f"         {self.server_info.get('name', '?')} "
                f"{self.server_info.get('version', '')} · protocol {self.protocol_version or '?'}"
            )
        if self.error:
            out.append(f"         {self.error}")
        for tool in self.tools:
            first = (tool.description or "").splitlines()[0] if tool.description else ""
            out.append(f"         {tool.exposed}{('  ' + first[:90]) if first else ''}")
        return out

    def name_line(self) -> str:
        return f"{self.spec.name} ({self.spec.transport}: {self.spec.describe()})"


def _flatten(content: Any) -> str:
    """MCP content blocks -> one text blob."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, indent=2, ensure_ascii=False)
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text") or ""))
            elif kind == "resource":
                resource = item.get("resource") or {}
                uri = resource.get("uri") or "?"
                if resource.get("text"):
                    parts.append(f"[{uri}]\n{resource['text']}")
                else:
                    parts.append(f"[{uri}] (binary resource, {item.get('mimeType') or 'unknown type'})")
            elif kind == "image":
                parts.append(f"[image {item.get('mimeType') or '?'}, not rendered in the transcript]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# the registry the session holds
# ---------------------------------------------------------------------------
def default_config_path(root: Path | str | None = None) -> Path | None:
    """First MCP config that exists: root, then the user's config dir.

    The user-level candidate kept its old name through the rename
    (``~/.config/redtram/mcp.json``), because an operator's MCP server list
    is hand-written config they may have documented elsewhere; silently
    switching the path would hide it from them.
    """
    candidates = []
    if root:
        candidates.append(Path(root).expanduser() / "mcp.json")
    candidates.append(Path.home() / ".config" / "redtram" / "mcp.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_config(raw: Any) -> list[ServerSpec]:
    """``{mcpServers: {...}}`` (or a bare mapping) -> server specs."""
    if not isinstance(raw, dict):
        raise MCPError("the MCP config must be a JSON object")
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        raise MCPError("mcpServers must be an object of server names")
    specs: list[ServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if isinstance(command, list):
            argv = tuple(str(part) for part in command)
        elif isinstance(command, str) and command:
            argv = tuple([command, *(str(a) for a in entry.get("args") or [])])
        else:
            argv = ()
        url = str(entry.get("url") or entry.get("serverUrl") or "")
        if not argv and not url:
            continue
        spec = ServerSpec(
            name=str(name),
            command=argv,
            url=url,
            env={str(k): str(v) for k, v in (entry.get("env") or entry.get("headers") or {}).items()},
            cwd=str(entry.get("cwd") or ""),
            enabled=bool(entry.get("enabled", True)),
        )
        specs.append(spec)
    return specs


TEMPLATE = {
    "mcpServers": {
        "example": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {},
        }
    }
}


class MCPRegistry:
    """Every configured server, in one place, never fatal."""

    def __init__(self, config_path: str | Path | None = None, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else Path.cwd()
        self.path = Path(config_path).expanduser() if config_path else default_config_path(self.root)
        self.servers: list[MCPServer] = []
        self.config_error = ""

    # -- config -----------------------------------------------------------
    def load(self) -> None:
        self.close()
        self.config_error = ""
        if self.path is None:
            self.path = default_config_path(self.root)
        if self.path is None or not self.path.is_file():
            self.config_error = "no mcp.json found"
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.config_error = f"{self.path} could not be read: {exc}"
            return
        try:
            specs = parse_config(raw)
        except MCPError as exc:
            self.config_error = str(exc)
            return
        self.servers = [MCPServer(spec) for spec in specs if spec.enabled]

    def ensure_loaded(self) -> None:
        if not self.servers and not self.config_error:
            self.load()

    # -- lifecycle --------------------------------------------------------
    def start_all(self, timeout: float = START_TIMEOUT) -> None:
        """Bring every server up, concurrently, and never raise."""
        self.ensure_loaded()
        threads = [
            threading.Thread(target=server.start, args=(timeout,), name=f"mcp-start-{server.spec.name}")
            for server in self.servers if server.state != "ready"
        ]
        for thread in threads:
            thread.daemon = True
            thread.start()
        for thread in threads:
            thread.join(timeout + 5)

    def reload(self, timeout: float = START_TIMEOUT) -> None:
        self.load()
        self.start_all(timeout)

    def close(self) -> None:
        for server in self.servers:
            try:
                server.close()
            except Exception:  # noqa: BLE001 - shutting down must never raise
                pass
        self.servers = []

    def stop_all(self) -> None:
        """Disconnect without forgetting the configuration."""
        for server in self.servers:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass

    # -- tools ------------------------------------------------------------
    def tools(self) -> list[ToolSpec]:
        return [tool for server in self.servers if server.state == "ready" for tool in server.tools]

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function schemas for every connected tool."""
        schemas: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for tool in self.tools():
            name = tool.exposed
            if name in seen:
                # Two servers with the same name, or a truncated one: disambiguate.
                seen[name] += 1
                name = f"{name[:MAX_NAME - 3]}_{seen[name]}"
                tool.exposed = name
            seen[name] = 0
            schemas.append(tool.openai_schema())
        return schemas

    def tool_names(self) -> tuple[str, ...]:
        return tuple(schema["function"]["name"] for schema in self.schemas())

    def find(self, exposed: str) -> tuple[MCPServer, ToolSpec] | None:
        for server in self.servers:
            for tool in server.tools:
                if tool.exposed == exposed:
                    return server, tool
        return None

    def call(self, exposed: str, arguments: dict[str, Any]) -> ToolResult:
        self.ensure_loaded()
        found = self.find(exposed)
        if found is None:
            if self.servers:
                self.start_all()
                found = self.find(exposed)
            if found is None:
                known = ", ".join(self.tool_names()) or "none"
                return ToolResult(False, f"no MCP tool named {exposed!r}; connected tools: {known}")
        server, tool = found
        return server.call(tool.name, arguments)

    # -- status -----------------------------------------------------------
    @property
    def ready(self) -> int:
        return sum(1 for server in self.servers if server.state == "ready")

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else "",
            "error": self.config_error,
            "servers": [server.status() for server in self.servers],
            "tools": len(self.tools()),
        }

    def lines(self) -> list[str]:
        """The whole picture, for ``/mcp`` and the menu."""
        out: list[str] = []
        where = str(self.path) if self.path else "(no config file)"
        out.append(f"MCP config: {where}")
        if self.config_error:
            out.append(f"  {self.config_error}")
        if not self.servers:
            out.append("  no servers configured")
            out.append(
                '  add {"mcpServers": {"files": {"command": "npx", '
                '"args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}}} '
                "to that file, then reload"
            )
            return out
        for server in self.servers:
            out.extend(server.lines())
        out.append(f"{self.ready}/{len(self.servers)} servers ready · {len(self.tools())} tools")
        return out
