"""Minimal Ollama HTTP client, standard library only.

Pipeline calls are non-streaming: their output is captured whole for the run
record, and a partial reply is not a useful artifact. The interactive chat is
the exception and streams, because a local model is slow enough that silence
during generation reads as a hang.

Generation is allowed to run for an hour: on this box a 14B at a large context
can take minutes per call, and a client timeout is the one failure mode with no
telemetry to diagnose afterwards.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .config import RoleSpec, Sampling


class OllamaError(RuntimeError):
    """Ollama could not be reached, or answered with something unusable."""


def make_client(base_url: str, *, api_key: str = "", timeout: float = 3600.0):
    """Pick the client for an endpoint by its URL shape.

    An endpoint ending in ``/v1`` speaks OpenAI chat completions -- llama.cpp's
    server, vLLM, and hosted APIs all do. Anything else is treated as Ollama's
    native ``/api``. This is the one place the choice is made, so the pipeline,
    the chat front ends and the model menu cannot disagree about it.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return OpenAIClient(base, api_key=api_key, timeout=timeout)
    return OllamaClient(base, api_key=api_key, timeout=timeout)


@dataclass(frozen=True)
class ApiResponse:
    """A parsed response plus the exact body text.

    The body is kept verbatim because the raw response is archived per call and
    is the only record of what the server actually sent.
    """

    data: dict[str, Any]
    text: str


def _human_size(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    return f"{value / (1024 ** 3):.1f} GiB"


def format_model_row(row: dict[str, Any]) -> str:
    """One display line for a model, built only from the fields present.

    The two backends describe models differently — llama.cpp volunteers the
    loaded model's geometry, Ollama reports a string like ``8.2B`` — so every
    field is optional and a bare id is always a valid row. ``state`` is the
    only field a router server reports for a model it has never loaded, so it
    leads the line rather than trailing it.
    """
    name = str(row.get("id", ""))
    state = str(row.get("state") or "")
    prefix = f"[{state}] " if state else ""
    parts: list[str] = []

    params = row.get("params")
    if isinstance(params, int) and params > 0:
        parts.append(f"{params / 1e9:.1f}B params")
    elif isinstance(params, str) and params:
        parts.append(f"{params} params")

    if row.get("quant"):
        parts.append(str(row["quant"]))

    size = _human_size(row.get("size"))
    if size:
        parts.append(size)

    if row.get("ctx"):
        ctx = f"ctx {row['ctx']}"
        if row.get("ctx_train"):
            ctx += f" (trained {row['ctx_train']})"
        parts.append(ctx)

    return f"{prefix}{name}" if not parts else f"{prefix}{name}  —  {' · '.join(parts)}"


class OllamaClient:
    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 3600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _call(self, endpoint: str, payload: dict[str, Any] | None) -> ApiResponse:
        url = f"{self.base_url}{endpoint}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(  # noqa: S310 - operator-supplied URL
            url,
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise OllamaError(f"Ollama HTTP {exc.code} from {endpoint}:\n{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc

        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise OllamaError(
                f"Ollama response from {endpoint} was not JSON (first 400 chars):\n"
                f"{text[:400]}"
            ) from exc
        return ApiResponse(data=parsed, text=text)

    def version(self) -> str:
        """Server version, or raise if the server is unreachable."""
        return str(self._call("/api/version", None).data.get("version", "?"))

    @property
    def backend(self) -> str:
        """Short name for the serving stack, for banners and diagnostics."""
        return "ollama"

    def running(self) -> list[dict[str, Any]]:
        """Models currently resident, for diagnostics."""
        return list(self._call("/api/ps", None).data.get("models", []))

    def models(self) -> list[str]:
        """Installed model names, or raise if the server is unreachable."""
        entries = self._call("/api/tags", None).data.get("models", [])
        return [
            str(entry.get("name", ""))
            for entry in entries
            if isinstance(entry, dict) and entry.get("name")
        ]

    def model_details(self) -> list[dict[str, Any]]:
        """Names plus the thin metadata ``/api/tags`` reports."""
        rows: list[dict[str, Any]] = []
        for entry in self._call("/api/tags", None).data.get("models", []):
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            row: dict[str, Any] = {"id": str(entry["name"])}
            if entry.get("size"):
                row["size"] = entry["size"]
            details = entry.get("details")
            if isinstance(details, dict):
                if details.get("parameter_size"):
                    row["params"] = details["parameter_size"]
                if details.get("quantization_level"):
                    row["quant"] = details["quantization_level"]
            rows.append(row)
        return rows

    def pull(self, model: str) -> str:
        payload = {"model": model, "stream": False, "keep_alive": "10m"}
        return str(self._call("/api/pull", payload).data.get("status", "ready"))

    def generate(
        self,
        role: RoleSpec,
        prompt: str,
        sampling: Sampling,
        system: str | None = None,
    ) -> ApiResponse:
        """Run one non-streaming completion for ``role``.

        ``system`` is Ollama's system message slot, which is where a deployed
        model receives its stock system prompt. Passing it here rather than
        pasting the prompt into ``prompt`` keeps the deployment framing and the
        harness's own instructions on separate channels, the way they are
        separated in a real deployment.
        """
        options: dict[str, Any] = {
            "temperature": role.temperature,
            "top_p": sampling.top_p,
            "num_ctx": role.num_ctx,
            "num_predict": role.num_predict,
            "repeat_penalty": sampling.repeat_penalty,
        }
        if sampling.top_k > 0:
            options["top_k"] = sampling.top_k
        if sampling.num_gpu.strip() not in ("", "-1"):
            options["num_gpu"] = int(sampling.num_gpu)

        payload: dict[str, Any] = {
            "model": role.model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": sampling.keep_alive,
            "options": options,
        }
        if system is not None and system.strip():
            payload["system"] = system
        think = role.think_flag
        if think is not None:
            payload["think"] = think

        return self._call("/api/generate", payload)

    def show(self, model: str) -> dict[str, Any]:
        """Model metadata, including the advertised ``capabilities`` list."""
        return dict(self._call("/api/show", {"model": model}).data)

    def supports_native_tools(self, model: str) -> bool:
        """Whether ``model`` can be handed tool schemas through its template.

        Ollama derives the ``tools`` capability from the chat template, and an
        abliterated GGUF can advertise it while shipping a template with no tool
        branch at all -- so the template is what gets checked, not the claim.
        """
        try:
            info = self.show(model)
        except OllamaError:
            return False
        if "tools" not in (info.get("capabilities") or []):
            return False
        return "tools" in (info.get("template") or "")

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        think: bool | None = None,
        keep_alive: str = "10m",
    ) -> Iterator[dict[str, Any]]:
        """Stream one chat turn, yielding each parsed NDJSON chunk.

        The caller needs the deltas as they arrive, so this is a generator and
        the connection stays open until the model stops. Ollama reports transport
        or model errors as a chunk carrying ``error``, which is raised here
        rather than yielded, so a failed turn cannot look like an empty reply.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "options": options,
        }
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think

        url = f"{self.base_url}/api/chat"
        request = urllib.request.Request(  # noqa: S310 - operator-supplied URL
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError as exc:
                        raise OllamaError(
                            f"Ollama sent a non-JSON stream line: {line[:200]}"
                        ) from exc
                    if isinstance(chunk, dict) and chunk.get("error"):
                        raise OllamaError(str(chunk["error"]))
                    yield chunk
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise OllamaError(f"Ollama HTTP {exc.code} from /api/chat:\n{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc


class OpenAIClient:
    """A chat-completions client for any /v1-compatible endpoint.

    Shared with OllamaClient at the caller's layer: the chat engine and the
    menus treat either interchangeably and never branch on which one answered.
    """

    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 3600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, endpoint: str, payload: dict[str, Any] | None, stream: bool = False):
        url = f"{self.base_url}{endpoint}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - operator-supplied URL
            url, data=body, headers=self._headers(),
            method="POST" if body is not None else "GET",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise OllamaError(f"HTTP {exc.code} from {endpoint}: {detail}") from exc
        except urllib.error.URLError as exc:
            # The endpoint is down, wrong, or mid-restart. A bare urllib
            # traceback ("Errno 111 connection refused") tells the operator
            # nothing about which server or port is at fault, so name both.
            raise OllamaError(f"cannot reach {url}: {exc.reason}") from exc

    def version(self) -> str:
        # There is no version endpoint; a model listing works as the probe.
        with self._request("/models", None) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return str(data.get("object", "openai"))

    @property
    def backend(self) -> str:
        """Short name for the serving stack, for banners and diagnostics."""
        return "llama.cpp"

    def models(self) -> list[str]:
        """Model ids, for menus that need to populate from the endpoint."""
        with self._request("/models", None) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return [
            str(entry.get("id", ""))
            for entry in data.get("data", [])
            if isinstance(entry, dict) and entry.get("id")
        ]

    def model_details(self) -> list[dict[str, Any]]:
        """Model ids plus whatever geometry llama.cpp volunteers.

        A single-model ``llama-server`` reports one row; a *router* server
        (``--models-dir``) reports every GGUF it was pointed at and loads them
        on demand. An unloaded model carries a ``status`` but no ``meta`` —
        geometry is only known once it has been loaded — so ``state`` is the
        one field that is always meaningful.
        """
        with self._request("/models", None) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        rows: list[dict[str, Any]] = []
        for entry in data.get("data", []) or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            row: dict[str, Any] = {"id": str(entry["id"])}
            status = entry.get("status")
            if isinstance(status, dict):
                if status.get("value"):
                    row["state"] = str(status["value"])
            elif isinstance(status, str) and status:
                row["state"] = status
            source = entry.get("source")
            if isinstance(source, str) and source:
                row["source"] = source
            meta = entry.get("meta")
            if isinstance(meta, dict):
                for source, target in (
                    ("n_params", "params"),
                    ("size", "size"),
                    ("ftype", "quant"),
                    ("n_ctx", "ctx"),
                    ("n_ctx_train", "ctx_train"),
                ):
                    if meta.get(source) not in (None, "", 0):
                        row[target] = meta[source]
            rows.append(row)
        return rows

    def pull(self, model: str) -> str:
        """No-op: an OpenAI-compatible server loads its own model at startup.

        Kept so callers that ensure models are present before a run work
        unchanged; there is nothing to fetch over this API.
        """
        return "ready (server-managed)"

    def supports_native_tools(self, model: str) -> bool:
        """Assume the loaded server advertises tool support if it lists it.

        The endpoint exposes no per-model template, so the chat's ``auto``
        protocol falls back to text framing unless the caller forces native.
        """
        return False

    def generate(
        self,
        role: "RoleSpec",
        prompt: str,
        sampling: "Sampling",
        system: str | None = None,
    ) -> "ApiResponse":
        """Run one non-streaming chat completion for ``role``.

        This mimics OllamaClient.generate by using the chat completions endpoint
        with a user message (and optional system message).
        """
        messages: list[dict[str, Any]] = []
        if system is not None and system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        options: dict[str, Any] = {
            "temperature": role.temperature,
            "top_p": sampling.top_p,
            "max_tokens": role.num_predict,
        }
        if sampling.top_k > 0:
            options["top_k"] = sampling.top_k

        payload: dict[str, Any] = {
            "model": role.model,
            "messages": messages,
            "stream": False,
            **options,
        }
        if role.think_flag is not None:
            payload["reasoning"] = {"effort": "medium"} if role.think_flag else None

        with self._request("/chat/completions", payload) as response:
            text = response.read().decode("utf-8", "replace")
        parsed = json.loads(text)
        # Normalize to Ollama-like response format
        content = ""
        thinking = ""
        if "choices" in parsed and parsed["choices"]:
            choice = parsed["choices"][0]
            if "message" in choice:
                content = choice["message"].get("content", "")
                thinking = choice["message"].get("reasoning_content", "")
        normalized = {"response": content, "thinking": thinking}
        return ApiResponse(data=normalized, text=text)

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        think: bool | None = None,
        keep_alive: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Stream one chat turn, yielding OpenAI-style SSE chunks."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": options.get("temperature"),
            "top_p": options.get("top_p"),
            "max_tokens": options.get("num_predict"),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": schema["function"]["name"],
                        "description": schema["function"].get("description", ""),
                        "parameters": schema["function"].get("parameters", {}),
                    },
                }
                for schema in tools
            ]
        if think is not None:
            payload["reasoning"] = {"effort": "medium"} if think else None
        payload = {k: v for k, v in payload.items() if v is not None}

        with self._request("/chat/completions", payload, stream=True) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload_text = line[len("data:"):].strip()
                if payload_text == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload_text)
                except ValueError:
                    continue
                yield chunk