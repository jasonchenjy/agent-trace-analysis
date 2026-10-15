"""Codex SDK adapter. Discovery tests inject the same interface without a model."""

from dataclasses import dataclass
from importlib import import_module, metadata
import json
import tempfile
import time
from typing import Protocol


class LLMError(RuntimeError):
    def __init__(self, message, *, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class LLMClient(Protocol):
    provider: str

    @property
    def cache_identity(self) -> dict: ...

    def complete(self, *, model: str, instructions: str, payload: dict,
                 schema: dict, schema_name: str) -> str: ...


# Explicitly avoid repository context and external tools for trace analysis.
# Sandbox.read_only alone restricts writes, not reads of the whole filesystem.
CODEX_OVERRIDES = (
    "features.shell_tool=false", "features.apps=false", "features.multi_agent=false",
    'web_search="disabled"', "mcp_servers={}",
)


def _load_sdk():
    try:
        return import_module("openai_codex")
    except ImportError as exc:
        raise LLMError("openai-codex SDK is unavailable; install openai-codex==0.162.1", fatal=True) from exc


@dataclass
class CodexClient:
    max_attempts: int = 3
    provider: str = "openai_codex"

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be 1..5")

    @property
    def cache_identity(self):
        try:
            sdk_version = metadata.version("openai-codex")
        except metadata.PackageNotFoundError:
            sdk_version = "unavailable"
        return {"provider": self.provider, "sdk_version": sdk_version,
                "adapter_version": "codex-v1", "sandbox": "read_only",
                "output_mode": "output_schema", "config_overrides": list(CODEX_OVERRIDES)}

    def complete(self, *, model, instructions, payload, schema, schema_name):
        sdk = _load_sdk()
        prompt = json.dumps({"untrusted_data": payload}, ensure_ascii=True)
        for attempt in range(self.max_attempts):
            try:
                # Each analysis/retry gets a clean, ephemeral thread. The empty
                # cwd keeps repository instructions/reviewer files out of context.
                with tempfile.TemporaryDirectory(prefix="trace-discovery-") as workdir:
                    config = sdk.CodexConfig(cwd=workdir, config_overrides=CODEX_OVERRIDES)
                    with sdk.Codex(config=config) as codex:
                        thread = codex.thread_start(
                            model=model, sandbox=sdk.Sandbox.read_only,
                            approval_mode=sdk.ApprovalMode.deny_all, ephemeral=True,
                            cwd=workdir, base_instructions=instructions,
                        )
                        result = thread.run(prompt, output_schema=schema)
                status = getattr(result.status, "value", result.status)
                if status != "completed" or result.error is not None:
                    raise LLMError("Codex turn did not complete successfully; check Codex login and model access")
                if not isinstance(result.final_response, str) or not result.final_response.strip():
                    raise LLMError("Codex returned no final_response; no result accepted")
                return result.final_response
            except (sdk.ServerBusyError, sdk.TransportClosedError, TimeoutError, ConnectionError) as exc:
                if attempt + 1 == self.max_attempts:
                    raise LLMError(f"Codex transient request failed after {attempt + 1} attempt(s)") from exc
            except (sdk.CodexError, OSError) as exc:
                # SDK error text can contain runtime context. Keep credentials,
                # raw stderr and source data out of diagnostic artifacts.
                raise LLMError(
                    f"Codex runtime error ({type(exc).__name__}); check SDK installation, Codex login and model access",
                    fatal=True,
                ) from exc
            time.sleep(min(2 ** attempt, 4))
        raise LLMError("Codex request exhausted its retry budget")


# Compatibility for library callers of the original client name.
OpenAIClient = CodexClient
