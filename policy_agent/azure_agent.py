from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .models import PolicyAgentDraft, PolicyAgentInput, PolicyAgentOutput, TokenUsage
from .prompts import (
    governance_input_message,
    governance_instructions,
    policy_input_message,
    policy_instructions,
    repair_input_message,
    repair_instructions,
)


POLICY_AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = POLICY_AGENT_DIR.parent
ModelT = TypeVar("ModelT", bound=BaseModel)


class AzureConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AzureAgentResult:
    output: PolicyAgentOutput
    usage: TokenUsage


@dataclass(frozen=True)
class _Attempt:
    content: str
    usage: TokenUsage


class AzurePolicyAgents:
    """Mandatory Azure policy-reasoning and governance agents."""

    def __init__(self, endpoint: str, api_key: str, api_version: str, deployment: str, max_tokens: int) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError as error:
            raise AzureConfigurationError("Install policy_agent/requirements.txt before running.") from error

        self.deployment = deployment
        self.max_tokens = max_tokens
        self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)

    @classmethod
    def from_env(cls) -> "AzurePolicyAgents":
        load_azure_env()
        required = {
            "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
            "AZURE_OPENAI_API_KEY": os.getenv("AZURE_OPENAI_API_KEY"),
            "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        }
        invalid = [name for name, value in required.items() if not _usable_setting(name, value)]
        if invalid:
            raise AzureConfigurationError("Missing or placeholder Azure settings: " + ", ".join(invalid))
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")
        _validate_api_version(api_version)
        return cls(
            endpoint=required["AZURE_OPENAI_ENDPOINT"] or "",
            api_key=required["AZURE_OPENAI_API_KEY"] or "",
            api_version=api_version,
            deployment=required["AZURE_OPENAI_DEPLOYMENT"] or "",
            max_tokens=int(os.getenv("AZURE_OPENAI_MAX_OUTPUT_TOKENS", "2400")),
        )

    @staticmethod
    def config_status() -> dict[str, bool]:
        load_azure_env()
        return {
            name: _usable_setting(name, os.getenv(name))
            for name in (
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_DEPLOYMENT",
            )
        } | {
            "AZURE_OPENAI_API_VERSION": bool(os.getenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")),
        }

    def evaluate(self, policy_input: PolicyAgentInput, policy_context: str) -> AzureAgentResult:
        attempts: list[_Attempt] = []

        draft_attempt = self._request(
            policy_instructions(policy_context),
            policy_input_message(policy_input),
        )
        attempts.append(draft_attempt)
        draft = self._parse_or_repair(
            target="policy draft",
            model_type=PolicyAgentDraft,
            attempt=draft_attempt,
            policy_input=policy_input,
            policy_context=policy_context,
            attempts=attempts,
        )

        governance_attempt = self._request(
            governance_instructions(policy_context),
            governance_input_message(policy_input, draft),
        )
        attempts.append(governance_attempt)
        output = self._parse_or_repair(
            target="final output",
            model_type=PolicyAgentOutput,
            attempt=governance_attempt,
            policy_input=policy_input,
            policy_context=policy_context,
            attempts=attempts,
        )

        usage = TokenUsage(input_tokens=0, output_tokens=0)
        for attempt in attempts:
            usage = usage.add(attempt.usage)
        if usage.input_tokens <= 0 or usage.output_tokens <= 0:
            raise RuntimeError("Azure did not return positive input and output token usage.")
        return AzureAgentResult(output=output, usage=usage)

    def _parse_or_repair(
        self,
        *,
        target: str,
        model_type: type[ModelT],
        attempt: _Attempt,
        policy_input: PolicyAgentInput,
        policy_context: str,
        attempts: list[_Attempt],
    ) -> ModelT:
        try:
            parsed = model_type.model_validate_json(attempt.content)
            _validate_input_binding(parsed, policy_input)
            return parsed
        except (ValidationError, ValueError) as error:
            validation_error = str(error)

        repair = self._request(
            repair_instructions(target, policy_context),
            repair_input_message(target, policy_input, attempt.content, validation_error),
        )
        attempts.append(repair)
        try:
            parsed = model_type.model_validate_json(repair.content)
            _validate_input_binding(parsed, policy_input)
            return parsed
        except (ValidationError, ValueError) as error:
            raise RuntimeError(
                f"Azure returned invalid {target} after repair for {policy_input.case.trace_id}: {error}"
            ) from error

    def _request(self, instructions: str, input_text: str) -> _Attempt:
        response = self.client.responses.create(
            model=self.deployment,
            instructions=instructions,
            input=input_text,
            max_output_tokens=self.max_tokens,
            text={"format": {"type": "json_object"}},
        )
        content = getattr(response, "output_text", None) or _response_text(response)
        if not content:
            raise RuntimeError("Azure returned an empty response.")
        return _Attempt(content=content, usage=_response_usage(response))


def load_azure_env() -> None:
    _load_env_file(REPO_ROOT / ".env")
    _load_env_file(POLICY_AGENT_DIR / ".env")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _validate_input_binding(output: BaseModel, policy_input: PolicyAgentInput) -> None:
    case = getattr(output, "case")
    if case.trace_id != policy_input.case.trace_id:
        raise ValueError("output case.trace_id must match the input")
    if case.ticket_id != policy_input.case.ticket_id:
        raise ValueError("output case.ticket_id must match the input")
    if case.policy_version_used != policy_input.case.policy_version:
        raise ValueError("output case.policy_version_used must match the input")
    if getattr(output, "customer_request") != policy_input.customer_request:
        raise ValueError("output customer_request must exactly preserve the input")


def _response_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    input_tokens = _value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _value(usage, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        raise RuntimeError("Azure response did not contain token usage.")
    return TokenUsage(input_tokens=int(input_tokens), output_tokens=int(output_tokens))


def _value(value: Any, *names: str) -> Any:
    for name in names:
        result = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if result is not None:
            return result
    return None


def _response_text(response: Any) -> str | None:
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "".join(chunks) or None


def _usable_setting(name: str, value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if any(marker in normalized for marker in ("replace-me", "<your-", "your-resource")):
        return False
    if name == "AZURE_OPENAI_ENDPOINT":
        return normalized.startswith("https://") and ".openai.azure.com" in normalized
    return True


def _validate_api_version(api_version: str) -> None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", api_version)
    if not match or tuple(int(part) for part in match.groups()) < (2025, 3, 1):
        raise AzureConfigurationError(
            "AZURE_OPENAI_API_VERSION must be 2025-03-01-preview or later for the Responses API."
        )
