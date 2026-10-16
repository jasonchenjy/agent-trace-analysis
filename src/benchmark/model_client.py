"""Bounded process adapter around the existing CodexClient (no second SDK)."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from ..llm import CodexClient, LLMError


class BoundedCodexClient:
    provider = "openai_codex"

    def __init__(self, timeout_seconds=60):
        if type(timeout_seconds) not in (float, int) or not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be in (0, 600]")
        self.timeout_seconds = timeout_seconds

    @property
    def cache_identity(self):
        return {**CodexClient(max_attempts=1).cache_identity, "adapter_version": "benchmark-bounded-codex-v1",
                "timeout_seconds": self.timeout_seconds, "max_attempts": 1,
                "conversation_mode": "fresh_ephemeral_turn_with_full_public_history"}

    def complete(self, **kwargs):
        # No specification, oracle, review, state, or private event is sent to this worker.
        command = [sys.executable, "-m", "src.benchmark.model_worker"]
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, _ = process.communicate(json.dumps(kwargs), timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise TimeoutError("Codex action timeout") from exc
        if process.returncode:
            raise LLMError("Codex worker failed; check installation and login", fatal=True)
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise LLMError("Codex worker protocol error", fatal=True) from exc
        if "error" in result:
            raise LLMError(result["error"], fatal=True)
        return result["response"]
