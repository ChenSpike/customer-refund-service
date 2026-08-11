from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import ConfidenceEvidence, ConfidenceLevel, PrecedentEvidence, TokenUsage


POLICY_AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = POLICY_AGENT_DIR.parents[1]
ModelT = TypeVar("ModelT", bound=BaseModel)


class AzureConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AzureJsonResult(Generic[ModelT]):
    value: ModelT
    usage: TokenUsage


@dataclass(frozen=True)
class _Attempt:
    content: str
    usage: TokenUsage


class AzureJsonReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$")
    value_json: str


class AzureJsonRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacements: list[AzureJsonReplacement] = Field(min_length=1, max_length=50)


class PolicyConfidenceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: int = Field(ge=0, le=3)
    confidence_level: ConfidenceLevel
    confidence_evidence: ConfidenceEvidence
    precedent_evidence: PrecedentEvidence

    @model_validator(mode="after")
    def validate_level(self) -> "PolicyConfidenceCorrection":
        expected = {3: "high", 2: "moderate", 1: "low", 0: "insufficient"}[self.confidence]
        if self.confidence_level != expected:
            raise ValueError(f"confidence {self.confidence} requires confidence_level={expected}")
        return self


class AzurePolicyRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence_correction: PolicyConfidenceCorrection
    replacements: list[AzureJsonReplacement] = Field(default_factory=list, max_length=50)


class AzureJsonClient:
    """Mandatory Azure JSON generation with one Azure-based repair attempt."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
        max_tokens: int,
        temperature: float,
    ) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError as error:
            raise AzureConfigurationError("Install the repository requirements before running.") from error

        self.deployment = deployment
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)

    @classmethod
    def from_env(cls) -> "AzureJsonClient":
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
            temperature=float(os.getenv("AZURE_OPENAI_TEMPERATURE", "0")),
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
        } | {"AZURE_OPENAI_API_VERSION": bool(os.getenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview"))}

    def generate(
        self,
        *,
        target: str,
        instructions: str,
        input_text: str,
        model_type: type[ModelT],
        validate: Callable[[ModelT], None],
    ) -> AzureJsonResult[ModelT]:
        attempt = self._request(instructions, input_text, model_type=model_type)
        try:
            value = self._parse(model_type, attempt.content, validate)
            return AzureJsonResult(value, attempt.usage)
        except (ValidationError, ValueError) as error:
            validation_error = str(error)
            repair_context = getattr(error, "repair_context", None)

        repair_model: type[BaseModel] = AzurePolicyRepair if repair_context else AzureJsonRepair
        repair = self._request(
            _repair_instructions(target, repair_model, bool(repair_context)),
            _repair_input(
                target,
                instructions,
                input_text,
                attempt.content,
                validation_error,
                repair_context,
            ),
            model_type=repair_model,
            repair=True,
        )
        try:
            replacements = repair_model.model_validate_json(repair.content)
            repaired_json = _apply_json_repair(attempt.content, replacements)
            value = self._parse(model_type, repaired_json, validate)
        except (ValidationError, ValueError, TypeError) as error:
            raise RuntimeError(f"Azure returned invalid {target} after repair: {error}") from error
        return AzureJsonResult(value, attempt.usage.add(repair.usage))

    @staticmethod
    def _parse(model_type: type[ModelT], content: str, validate: Callable[[ModelT], None]) -> ModelT:
        value = model_type.model_validate_json(content)
        validate(value)
        return value

    def _request(
        self,
        instructions: str,
        input_text: str,
        *,
        model_type: type[BaseModel],
        repair: bool = False,
    ) -> _Attempt:
        request_options: dict[str, Any] = {
            "model": self.deployment,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": self.max_tokens,
            "temperature": self.temperature,
            "text": {"format": _strict_json_format(model_type)},
        }
        if repair:
            request_options.pop("temperature")
            request_options["reasoning"] = {"effort": "low"}
        response = self.client.responses.create(
            **request_options,
        )
        content = getattr(response, "output_text", None) or _response_text(response)
        if not content:
            raise RuntimeError("Azure returned an empty response.")
        return _Attempt(content, _response_usage(response))


def load_azure_env() -> None:
    _load_env_file(REPO_ROOT / ".env")
    _load_env_file(POLICY_AGENT_DIR / ".env")


def _repair_instructions(
    target: str,
    repair_model: type[BaseModel],
    has_confidence_context: bool,
) -> str:
    schema = json.dumps(repair_model.model_json_schema(), indent=2)
    confidence_instruction = (
        "Fill the complete confidence_correction from authoritative_confidence exactly. Use replacements only for "
        "remaining non-confidence issues. "
        if has_confidence_context
        else ""
    )
    return (
        f"You are the Azure JSON repair step for {target}. Return only the smallest set of RFC 6901 JSON Pointer "
        "replacements needed to make invalid_json pass every issue in validation_error. Each path must already exist "
        "in invalid_json. Encode the replacement value as valid JSON text in value_json. Do not return the full target. "
        "Apply every issue in validation_error literally. A deterministic expected value, count, phrase, route, or "
        "decision named by the "
        "validator is authoritative and overrides conflicting prose or arithmetic in invalid_json. Preserve unaffected "
        "facts, evidence, policy fields, and explanations. Update all values that directly depend on a correction, "
        "including discrete confidence, level, evidence, decision, refund amount, gaps, and guidance when relevant. "
        f"{confidence_instruction}"
        "Use only the original input and stated constraints. Do not invent policy IDs, precedent IDs, evidence, or tool "
        "actions. Verify every listed issue before returning. "
        "Return JSON only with no wrapper, comments, or extra fields.\n\n"
        f"Required repair schema:\n{schema}"
    )


def _repair_input(
    target: str,
    original_instructions: str,
    original_input: str,
    invalid_json: str,
    validation_error: str,
    repair_context: dict[str, Any] | None,
) -> str:
    return json.dumps(
        {
            "target": target,
            "authoritative_confidence": repair_context,
            "validation_error": validation_error,
            "invalid_json": invalid_json,
            "original_input": original_input,
            "original_instructions": original_instructions,
        },
        indent=2,
        ensure_ascii=False,
    )


def _strict_json_format(model_type: type[BaseModel]) -> dict[str, Any]:
    schema = _strict_schema(model_type.model_json_schema())
    _add_confidence_levels(schema)
    return {
        "type": "json_schema",
        "name": re.sub(r"(?<!^)(?=[A-Z])", "_", model_type.__name__).lower(),
        "schema": schema,
        "strict": True,
    }


def _strict_schema(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _strict_schema(item) for key, item in value.items()}
        if "properties" in result:
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    return value


def _apply_json_repair(
    invalid_json: str,
    repair: AzureJsonRepair | AzurePolicyRepair,
) -> str:
    document = json.loads(invalid_json)
    if isinstance(repair, AzurePolicyRepair):
        correction = repair.confidence_correction
        decision = document.get("decision")
        if not isinstance(decision, dict):
            raise ValueError("policy confidence repair requires an existing decision object")
        decision["confidence"] = correction.confidence
        decision["confidence_level"] = correction.confidence_level
        decision["confidence_evidence"] = correction.confidence_evidence.model_dump(mode="json")
        decision["precedent_evidence"] = correction.precedent_evidence.model_dump(mode="json")
    seen_paths: set[str] = set()
    for replacement in repair.replacements:
        if replacement.path in seen_paths:
            raise ValueError(f"repair contains duplicate path {replacement.path}")
        seen_paths.add(replacement.path)
        segments = [segment.replace("~1", "/").replace("~0", "~") for segment in replacement.path[1:].split("/")]
        target = document
        for segment in segments[:-1]:
            if isinstance(target, list):
                target = target[int(segment)]
            elif isinstance(target, dict) and segment in target:
                target = target[segment]
            else:
                raise ValueError(f"repair path does not exist: {replacement.path}")
        final = segments[-1]
        value = json.loads(replacement.value_json)
        if isinstance(target, list):
            index = int(final)
            if index < 0 or index >= len(target):
                raise ValueError(f"repair path does not exist: {replacement.path}")
            target[index] = value
        elif isinstance(target, dict) and final in target:
            target[final] = value
        else:
            raise ValueError(f"repair path does not exist: {replacement.path}")
    return json.dumps(document, ensure_ascii=False)


def _add_confidence_levels(schema: dict[str, Any]) -> None:
    definitions = schema.get("$defs", {})
    target_name = None
    target_property = None
    for definition_name, property_name in (
        ("PolicyDecision", "decision"),
        ("PolicyConfidenceCorrection", "confidence_correction"),
    ):
        if definition_name in definitions and property_name in schema.get("properties", {}):
            target_name = definition_name
            target_property = property_name
            break
    if target_name is None or target_property is None:
        return
    decision = definitions[target_name]
    branches = []
    for score, level in (
        (3, "high"),
        (2, "moderate"),
        (1, "low"),
        (0, "insufficient"),
    ):
        branch = deepcopy(decision)
        branch.pop("title", None)
        branch["properties"]["confidence"] = {"type": "integer", "const": score}
        branch["properties"]["confidence_level"] = {"type": "string", "const": level}
        branches.append(branch)
    schema["properties"][target_property] = {"anyOf": branches}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _response_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    input_tokens = _value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _value(usage, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        raise RuntimeError("Azure response did not contain token usage.")
    result = TokenUsage(input_tokens=int(input_tokens), output_tokens=int(output_tokens))
    if result.input_tokens <= 0 or result.output_tokens <= 0:
        raise RuntimeError("Azure did not return positive input and output token usage.")
    return result


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
