"""Provider adapters. Adding a provider (for example a Gemini CLI in 0.2) means adding
one class here and one entry in `build_providers`.

CLI providers run tool-less and sandboxed with the prompt on stdin, never argv. They use
the CLI's existing login; coldtake never reads, copies or stores login tokens.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from .config import Config, PaidModel
from .failures import ProviderFailure, classify, error_text, redact

MAX_OUTPUT_BYTES = 4_000_000
# Environment passed to CLIs. Secrets such as OPENROUTER_API_KEY are never forwarded.
_PASS_ENV = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
)  # fmt: skip


@dataclass(frozen=True)
class Route:
    provider: str
    family: str
    model: str | None
    effort: str | None = None
    billing: Literal["subscription", "paid"] = "subscription"
    # Paid routes only: price ceilings in USD per million tokens and output cap.
    prompt_usd_per_mtok: float = 0.0
    completion_usd_per_mtok: float = 0.0
    max_output_tokens: int = 0
    structured_outputs: bool = True
    input_reserve_tokens: int = 4000
    context_tokens: int | None = None
    endpoints_only: tuple[str, ...] | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model or 'default'}"

    @classmethod
    def paid(cls, model: PaidModel) -> Route:
        return cls(
            "openrouter", model.family, model.model, None, "paid",
            model.prompt_usd_per_mtok, model.completion_usd_per_mtok,
            model.max_output_tokens, model.structured_outputs, model.input_reserve_tokens,
            model.context_tokens, model.endpoints_only,
        )  # fmt: skip


@dataclass
class Completion:
    data: dict[str, Any]
    cost_usd: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    # Paid routes: {"input_tokens": int, "output_tokens": int}, stored with the attempt.
    usage: dict[str, int] = field(default_factory=dict)
    raw: str | None = None  # the reply text, for repair prompts


class Provider(Protocol):
    """Interface of every provider, built-in or registered by an extension.

    `complete` returns a parsed JSON object or raises `ProviderFailure` whose `kind`
    tells the engine what to do (see failures.py). A provider whose routes have
    `billing="paid"` must raise `charge_unknown` for any failure after the request may
    have been accepted. It may raise `fallback`/`terminal` only for a provable
    pre-acceptance rejection and must then say so with `proven_unsent=True`; the engine
    treats any other paid failure as `charge_unknown` and never releases its allowance.
    Routes are described by `Route`; the provider only executes them.
    """

    name: str

    def available(self) -> bool: ...

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion: ...

    # Optional, looked up with getattr by the engine:
    #   worst_case_cost(route, prompt_bytes) -> float   replaces the default estimate
    #   context_fits(route, prompt_bytes) -> bool       replaces the default fit check


def load_object(path: str) -> Any:
    """Import "package.module:attribute"."""
    module, _, attribute = path.partition(":")
    if not module or not attribute:
        raise ValueError(f"expected 'package.module:attribute', got {path!r}")
    return getattr(importlib.import_module(module), attribute)


# -- output parsing ----------------------------------------------------------


def extract_json_object(text: str) -> dict[str, Any]:
    """The whole text if it is a JSON object, else the last JSON object inside it."""
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder, found, index = json.JSONDecoder(), None, 0
    while (index := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(value, dict):
            found = value
        index = end
    if found is None:
        raise ProviderFailure(
            "invalid_output",
            "repair",
            "no JSON object in provider output",
            cause_type="NoJSON",
            raw=text,
        )
    return found


# -- CLI providers -----------------------------------------------------------


class ProcessGroup:
    """A CLI process and its process group. The group is signalled only while the leader
    has not been reaped: once it is reaped its pid could belong to an unrelated process."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.reaped = False
        self.lock = threading.Lock()

    def kill(self) -> None:
        with self.lock:
            if not self.reaped:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.process.pid, signal.SIGKILL)

    def reap(self) -> None:
        with self.lock:
            if not self.reaped:
                self.process.wait()
                self.reaped = True

    def exited(self) -> bool:
        """True once the leader has exited, without reaping it (its pid stays reserved)."""
        with self.lock:
            if self.reaped:
                return True
            info = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return info is not None


_Group = ProcessGroup  # former private name, kept as an alias
_LIVE_GROUPS: set[ProcessGroup] = set()
_LIVE_LOCK = threading.Lock()


@contextlib.contextmanager
def live_process_group(process: subprocess.Popen[bytes]):
    """Track a spawned CLI process group for shutdown; kills stragglers and reaps on exit."""
    group = ProcessGroup(process)
    with _LIVE_LOCK:
        _LIVE_GROUPS.add(group)
    try:
        yield group
    finally:
        group.kill()  # stragglers must not outlive the attempt (no-op once reaped)
        group.reap()
        with _LIVE_LOCK:
            _LIVE_GROUPS.discard(group)


def terminate_live_processes() -> int:
    """Kill every CLI process group still running (used on shutdown after the drain)."""
    with _LIVE_LOCK:
        groups = list(_LIVE_GROUPS)
    for group in groups:
        group.kill()
    return len(groups)


class CLIProvider:
    name = ""

    def __init__(self, binary: str | None = None):
        self._configured = binary

    @property
    def executable(self) -> str | None:
        if self._configured:
            return self._configured if os.access(self._configured, os.X_OK) else None
        return shutil.which(self.name)

    def available(self) -> bool:
        return self.executable is not None

    @staticmethod
    def _environment() -> dict[str, str]:
        return {key: os.environ[key] for key in _PASS_ENV if key in os.environ}

    def _run(self, command: list[str], prompt: str, timeout: float, cwd: str) -> tuple[str, str]:
        """Run to completion; return (stdout, stderr) or raise a classified failure.
        Output is read as a stream and the process group is killed if it exceeds the bound."""
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._environment(), cwd=cwd, start_new_session=True,
            )  # fmt: skip
        except OSError as exc:
            raise ProviderFailure(
                "unavailable", "fallback", f"cannot start {self.name}: {exc}",
                cause_type=type(exc).__name__,
            ) from exc  # fmt: skip
        with live_process_group(process) as group:
            stdout, stderr = self.communicate(group, prompt.encode("utf-8"), timeout)
        out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        if process.returncode != 0:
            raise self._failure(out, err, prompt)
        return out, err

    def _communicate(
        self, group: ProcessGroup, payload: bytes, timeout: float
    ) -> tuple[bytes, bytes]:
        """Former private name of `communicate`."""
        return self.communicate(group, payload, timeout)

    def communicate(
        self,
        group: ProcessGroup,
        payload: bytes,
        timeout: float,
        *,
        max_output_bytes: int | None = None,
    ) -> tuple[bytes, bytes]:
        """Feed `payload` to the process on stdin and read both output streams to the end,
        bounded in size and time. Raises `output_too_large` or `timed_out`; kills the group
        on every exit. A subclass may override it to change how output is collected."""
        limit = MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
        process = group.process
        buckets: tuple[list[bytes], list[bytes]] = ([], [])
        total = [0]
        lock = threading.Lock()
        overflow = threading.Event()

        def pump(stream: Any, bucket: list[bytes]) -> None:
            while chunk := os.read(stream.fileno(), 65_536):
                with lock:
                    total[0] += len(chunk)
                    if total[0] > limit:
                        overflow.set()
                        return
                    bucket.append(chunk)

        def feed() -> None:
            with contextlib.suppress(OSError):
                process.stdin.write(payload)  # type: ignore[union-attr]
                process.stdin.close()  # type: ignore[union-attr]

        threads = [
            threading.Thread(target=pump, args=(process.stdout, buckets[0]), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, buckets[1]), daemon=True),
            threading.Thread(target=feed, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            deadline = time.monotonic() + timeout
            while not group.exited():
                if overflow.is_set():
                    group.kill()
                    group.reap()
                    raise ProviderFailure(
                        "output_too_large", "terminal", "provider output over bound"
                    )
                if time.monotonic() >= deadline:
                    group.kill()
                    group.reap()
                    raise ProviderFailure(
                        "timed_out", "timed_out", f"{self.name} exceeded {timeout:.0f}s",
                        cause_type="TimeoutExpired",
                    )  # fmt: skip
                time.sleep(0.05)
            # The leader exited but is not yet reaped, so its group id is still ours: kill
            # any grandchild that holds a pipe open, then reap.
            group.kill()
            group.reap()
            for thread in threads:
                thread.join(5)
            if overflow.is_set():
                raise ProviderFailure("output_too_large", "terminal", "provider output over bound")
        finally:
            group.kill()
            group.reap()
            for thread in threads:
                thread.join(5)
            for stream in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError, ValueError):
                    stream.close()  # type: ignore[union-attr]
        return b"".join(buckets[0]), b"".join(buckets[1])

    def _failure(self, stdout: str, stderr: str, prompt: str = "") -> ProviderFailure:
        """Classify from the provider's own error lines, never from an echoed prompt."""
        return classify(error_text(stdout, stderr, prompt))


class ClaudeProvider(CLIProvider):
    name = "claude"

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion:
        command = [
            self.executable or self.name, "--print", "--output-format", "json",
            "--model", route.model or "opus", "--effort", route.effort or "high",
            "--no-session-persistence", "--safe-mode", "--setting-sources", "",
            "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]  # fmt: skip
        with tempfile.TemporaryDirectory(prefix="coldtake-") as cwd:
            stdout, _ = self._run(command, prompt, timeout, cwd)
        try:
            wrapper = json.loads(stdout)
        except ValueError as exc:
            raise ProviderFailure(
                "invalid_output", "repair", "claude output is not JSON", cause_type="NoJSON"
            ) from exc
        if not isinstance(wrapper, dict):
            raise ProviderFailure("invalid_output", "repair", "claude output is not an object")
        if wrapper.get("is_error") is True:
            raise classify(str(wrapper.get("result", "")))
        value = wrapper.get("structured_output")
        if not isinstance(value, dict):
            result = wrapper.get("result")
            if not isinstance(result, str):
                raise ProviderFailure("invalid_output", "repair", "claude returned no result")
            value = extract_json_object(result)
        return Completion(value, raw=stdout)

    def _failure(self, stdout: str, stderr: str, prompt: str = "") -> ProviderFailure:
        try:
            wrapper = json.loads(stdout)
        except ValueError:
            return super()._failure(stdout, stderr, prompt)
        if isinstance(wrapper, dict) and wrapper.get("is_error") is True:
            return classify(str(wrapper.get("result", "")))
        return super()._failure(stdout, stderr, prompt)


class CodexProvider(CLIProvider):
    name = "codex"

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion:
        with tempfile.TemporaryDirectory(prefix="coldtake-") as directory:
            schema_path, output_path = Path(directory) / "schema.json", Path(directory) / "out.json"
            schema_path.write_text(json.dumps(schema, separators=(",", ":")))
            output_path.touch(mode=0o600)
            command = [
                self.executable or self.name, "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only",
            ]  # fmt: skip
            if route.model:
                command += ["--model", route.model]
            command += [
                "--config", f'model_reasoning_effort="{route.effort or "high"}"',
                "--output-schema", str(schema_path), "-o", str(output_path), "-",
            ]  # fmt: skip
            self._run(command, prompt, timeout, directory)
            text = _read_private(output_path)
            return Completion(extract_json_object(text), raw=text)


class GeminiProvider(CLIProvider):
    """Generic Gemini CLI provider (read-only/sandboxed single-turn print mode, prompt on stdin)."""

    name = "gemini"

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion:
        del (
            schema
        )  # generic gemini CLI enforces structure via prompt instructions + local validation
        command = [self.executable or self.name, "--output-format", "json"]
        if route.model:
            command += ["--model", route.model]
        with tempfile.TemporaryDirectory(prefix="coldtake-") as cwd:
            os.chmod(cwd, 0o700)
            stdout, _ = self._run(command, prompt, timeout, cwd)
        try:
            wrapper = json.loads(stdout)
        except ValueError:
            return Completion(extract_json_object(stdout), raw=stdout)
        if isinstance(wrapper, dict):
            if wrapper.get("error"):
                raise classify(str(wrapper["error"]))
            for key in ("structured_output", "response", "result"):
                val = wrapper.get(key)
                if isinstance(val, dict):
                    return Completion(val, raw=stdout)
                if isinstance(val, str):
                    return Completion(extract_json_object(val), raw=stdout)
        return Completion(extract_json_object(stdout), raw=stdout)


def _read_private(path: Path) -> str:
    """Read a file we created, refusing anything that is not a private regular file."""
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise ProviderFailure("unsafe_output", "terminal", "unsafe provider output file")
    if info.st_size > MAX_OUTPUT_BYTES:
        raise ProviderFailure("output_too_large", "terminal", "provider output over bound")
    return path.read_text(encoding="utf-8", errors="replace")


# -- paid provider -----------------------------------------------------------

# Failures where the HTTP layer proves the request was rejected before inference. With the
# one body-matched case below (503), this is the whole allow-list: any other status after
# the request was sent (502, 408 and every other 5xx included) is ambiguous by policy and is
# `charge_unknown`.
_REJECTED = {
    401: ("auth_expired", "fallback"),
    402: ("credit_limit", "fallback"),
    403: ("forbidden", "fallback"),
    429: ("rate_limited", "fallback"),
    400: ("invalid_input", "terminal"),
    404: ("not_found", "terminal"),
    413: ("invalid_input", "terminal"),
    422: ("invalid_input", "terminal"),
}


def routing_rejection(text: str) -> str | None:
    """The message of OpenRouter's documented "no provider meets your routing requirements"
    error (HTTP 503, raised before any inference), or None. Matched conservatively on the
    documented shape, `{"error": {"message": "No available model provider meets your
    routing requirements", ...}}`; any other 503 body is not this error."""
    try:
        error = json.loads(text)["error"]
        message = error["message"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(error, dict) or not isinstance(message, str):
        return None
    low = " ".join(message.casefold().split())
    if "no available model provider" in low and "routing requirements" in low:
        return message
    return None


_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


def worst_case_cost(
    route: Route, prompt_bytes: int, input_reserve_tokens: int | None = None
) -> float:
    """Upper bound in USD: bytes bound the token count from above (a token is at least a
    byte), plus a fixed reserve for chat framing, plus the full output allowance."""
    reserve = route.input_reserve_tokens if input_reserve_tokens is None else input_reserve_tokens
    exact = (
        Decimal(prompt_bytes + reserve) * Decimal(str(route.prompt_usd_per_mtok))
        + Decimal(route.max_output_tokens) * Decimal(str(route.completion_usd_per_mtok))
    ) / Decimal(1_000_000)
    return math.nextafter(float(exact), math.inf)  # round outwards


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._transport = transport

    def available(self) -> bool:
        return bool(self._key)

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion:
        """One guarded request: no retries, no model substitution, bounded output, hard
        price ceilings. Anything ambiguous after the request left is `charge_unknown`."""
        if route.billing != "paid" or not self._key:
            raise RuntimeError("OpenRouter provider needs a paid route and an API key")
        body: dict[str, Any] = {
            "model": route.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": route.max_output_tokens,
            "n": 1,
            "stream": False,
            "usage": {"include": True},
            "provider": {
                "max_price": {
                    "prompt": route.prompt_usd_per_mtok,
                    "completion": route.completion_usd_per_mtok,
                    "request": 0,
                },
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        if route.endpoints_only:
            body["provider"]["only"] = list(route.endpoints_only)
        if route.structured_outputs:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "coldtake", "strict": True, "schema": schema},
            }
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        status, text = self._post_with_deadline(body, headers, timeout)
        return self._interpret(status, text)

    def _post_with_deadline(
        self, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        """One POST bounded by a total wall-clock limit (httpx timeouts apply per phase).
        If the limit passes after the request may have left, that is charge-ambiguous."""
        outcome: dict[str, Any] = {}

        def send() -> None:
            try:
                outcome["ok"] = self._send(body, headers, timeout)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller thread
                outcome["error"] = exc

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise ProviderFailure(
                "charge_unknown", "charge_unknown",
                f"no complete response within {timeout:.0f}s; the request may have been accepted",
                cause_type="WallClockTimeout",
            )  # fmt: skip
        if "error" in outcome:
            raise outcome["error"]
        return outcome["ok"]

    def _send(
        self, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        chunks: list[bytes] = []
        size = 0
        try:
            with (
                httpx.Client(
                    transport=self._transport, timeout=timeout, follow_redirects=False
                ) as client,
                client.stream(
                    "POST", f"{self._base}/chat/completions", json=body, headers=headers
                ) as response,
            ):
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        raise ProviderFailure(
                            "charge_unknown", "charge_unknown", "response over the size bound",
                            cause_type="ResponseTooLarge",
                        )  # fmt: skip
                    chunks.append(chunk)
                status = response.status_code
        except _NEVER_SENT as exc:
            raise ProviderFailure(
                "unavailable", "fallback", f"request not sent: {exc}",
                cause_type=type(exc).__name__, proven_unsent=True,
            ) from exc  # fmt: skip
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "charge_unknown", "charge_unknown", f"request may have been accepted: {exc}",
                cause_type=type(exc).__name__,
            ) from exc  # fmt: skip
        return status, b"".join(chunks).decode("utf-8", "replace")

    def _interpret(self, status: int, text: str) -> Completion:
        if status != 200:
            if status in _REJECTED:
                reason, kind = _REJECTED[status]
                raise ProviderFailure(
                    reason, kind, f"HTTP {status}: {redact(text, 200)}",
                    cause_type=f"HTTP{status}", proven_unsent=True,
                )  # fmt: skip
            matched = routing_rejection(text) if status == 503 else None
            if matched is not None:
                raise ProviderFailure(
                    "unavailable", "fallback",
                    f"HTTP 503, no provider meets the routing requirements: {redact(matched, 200)}",
                    cause_type="HTTP503NoProvider", proven_unsent=True,
                )  # fmt: skip
            raise ProviderFailure(
                "charge_unknown", "charge_unknown", f"HTTP {status} after the request was sent",
                cause_type=f"HTTP{status}",
            )  # fmt: skip
        try:
            payload = json.loads(text)
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderFailure(
                "charge_unknown", "charge_unknown", "HTTP 200 without a usable completion",
                cause_type=type(exc).__name__,
            ) from exc  # fmt: skip
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        cost = _reported_cost(usage.get("cost"))
        detail = {"finish_reason": choice.get("finish_reason"), "model": payload.get("model")}
        usage_tokens = {
            key: usage[name]
            for key, name in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
            )
            if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
        }
        if choice.get("finish_reason") == "length" or not isinstance(content, str):
            raise ProviderFailure(
                "invalid_output", "repair", "completion truncated or empty",
                cause_type="Truncated", accepted=True, cost_usd=cost,
                raw=content if isinstance(content, str) else None, usage=usage_tokens,
            )  # fmt: skip
        try:
            data = extract_json_object(content)
        except ProviderFailure as exc:
            exc.accepted, exc.cost_usd, exc.raw, exc.usage = True, cost, content, usage_tokens
            raise
        return Completion(data, cost, detail, usage_tokens, content)


def _reported_cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


BUILTIN_PROVIDERS = frozenset({"claude", "codex", "gemini", "openrouter"})


def build_providers(
    config: Config,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Provider]:
    """Built-in providers plus extensions.

    An extension provider is a class or factory called as `factory(config, env)` and
    registered under its instance's `name`, via the `coldtake.providers` entry-point
    group or the `providers` config mapping. Only the config mapping may replace a
    built-in (by naming it), so replacing one is always an explicit operator choice.
    """
    env = os.environ if env is None else env
    providers: dict[str, Provider] = {
        "claude": ClaudeProvider(config.binaries.get("claude")),
        "codex": CodexProvider(config.binaries.get("codex")),
        "gemini": GeminiProvider(config.binaries.get("gemini")),
        "openrouter": OpenRouterProvider(
            env.get("OPENROUTER_API_KEY"), config.openrouter.base_url, transport
        ),
    }
    for ep in importlib.metadata.entry_points(group="coldtake.providers"):
        provider = ep.load()(config, env)
        if provider.name in providers:
            raise ValueError(f"provider name {provider.name!r} is already registered")
        providers[provider.name] = provider
    # The config mapping may also REPLACE a built-in by name (for example a subclass with
    # different failure classification); the instance's name must match its key.
    for name, path in config.providers.items():
        provider = load_object(path)(config, env)
        if provider.name != name:
            raise ValueError(f"provider {path!r} is named {provider.name!r}, not {name!r}")
        if name not in BUILTIN_PROVIDERS and name in providers:
            raise ValueError(f"provider name {name!r} is already registered")
        providers[name] = provider
    return providers
