"""Pydantic models for eiDOS trust boundaries.

The app stays flat and mostly dictionary/dataclass based internally. This module
only validates data as it crosses external or durable boundaries: config/env,
dashboard POST bodies, and JSON state records.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _format_validation_error(error: ValidationError) -> str:
    parts = []
    for entry in error.errors():
        loc = ".".join(str(item) for item in entry.get("loc", ())) or "value"
        parts.append(f"{loc}: {entry.get('msg', 'invalid value')}")
    return "; ".join(parts)


class BoundaryValidationError(ValueError):
    """Raised when a trust-boundary value fails Pydantic validation."""


class _StrictBoundaryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class _CompatStateRecord(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        strict=True,
        str_strip_whitespace=True,
    )


# --- Config + env ---------------------------------------------------------


class EidosEnvOverrides(BaseSettings):
    """Validated process-env overrides for load_config()."""

    model_config = SettingsConfigDict(env_prefix="EIDOS_", extra="ignore")

    llm_url: str | None = None
    workspace: str | None = None
    mock: bool = False

    @field_validator("llm_url", "workspace")
    @classmethod
    def _optional_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value


class LlmConfigSection(_StrictBoundaryModel):
    url: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    request_timeout_s: float | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    repeat_penalty: float | None = Field(default=None, gt=0)
    grammar_enabled: bool | None = None


class TickConfigSection(_StrictBoundaryModel):
    interval_s: float | None = Field(default=None, gt=0)
    interval_active_s: float | None = Field(default=None, gt=0)
    loop_detect_window: int | None = Field(default=None, ge=1)


class SafetyConfigSection(_StrictBoundaryModel):
    cmd_timeout_s: float | None = Field(default=None, gt=0)
    cmd_async_ceiling_s: float | None = Field(default=None, gt=0)
    bg_job_max_age_s: float | None = Field(default=None, gt=0)
    disk_min_gb: float | None = Field(default=None, ge=0)
    ram_max_pct: float | None = Field(default=None, gt=0, le=100)
    bg_output_max_bytes: int | None = Field(default=None, ge=1)
    protected_patterns: list[str] | None = None

    @field_validator("protected_patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for pattern in value:
            if not pattern.strip():
                raise ValueError("protected_patterns entries must not be empty")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid regex {pattern!r}: {error}") from error
        return value


class DashboardConfigSection(_StrictBoundaryModel):
    port: int | None = Field(default=None, ge=1, le=65535)
    voice_port: int | None = Field(default=None, ge=1, le=65535)
    token: str | None = None


class PathsConfigSection(_StrictBoundaryModel):
    workspace: str | None = Field(default=None, min_length=1)


class DelegateConfigSection(_StrictBoundaryModel):
    enabled: bool | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    allowed_dirs: list[str] | None = None
    max_sessions: int | None = Field(default=None, ge=1)
    pi_path: str | None = None
    pi_provider: str | None = None
    pi_model: str | None = None


class IdeConfigSection(_StrictBoundaryModel):
    enabled: bool | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    pi_provider: str | None = None
    pi_model: str | None = None
    max_stints: int | None = Field(default=None, ge=1)
    stint_idle_timeout_s: float | None = Field(default=None, gt=0)


class ConfigDocument(_StrictBoundaryModel):
    """Typed subset of config.toml.

    Top-level extras stay allowed for unmodeled legacy sections, while modeled
    safety-sensitive sections reject unknown keys.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    creature_mode: bool | None = None
    creature_shell: Literal["wsl", "powershell"] | None = None
    creature_wsl_distro: str | None = None
    llm: LlmConfigSection | None = None
    tick: TickConfigSection | None = None
    safety: SafetyConfigSection | None = None
    dashboard: DashboardConfigSection | None = None
    paths: PathsConfigSection | None = None
    delegate: DelegateConfigSection | None = None
    ide: IdeConfigSection | None = None


class ResolvedConfigBoundary(_StrictBoundaryModel):
    llm_url: str = Field(min_length=1)
    workspace_dir: str = Field(min_length=1)
    cmd_timeout_s: float = Field(gt=0)
    cmd_async_ceiling_s: float = Field(gt=0)
    bg_job_max_age_s: float = Field(gt=0)
    dashboard_port: int = Field(ge=1, le=65535)
    voice_port: int = Field(ge=1, le=65535)
    protected_patterns: list[str]

    @field_validator("protected_patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("protected_patterns must not be empty")
        SafetyConfigSection(protected_patterns=value)
        return value


def validate_config_document(data: Mapping[str, Any] | None, source: str) -> ConfigDocument:
    try:
        return ConfigDocument.model_validate(data or {})
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid {source}: {_format_validation_error(error)}"
        ) from error


def load_env_overrides() -> EidosEnvOverrides:
    try:
        return EidosEnvOverrides()
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid environment overrides: {_format_validation_error(error)}"
        ) from error


def validate_resolved_config(config: Any) -> ResolvedConfigBoundary:
    data = {
        "llm_url": getattr(config, "llm_url", ""),
        "workspace_dir": getattr(config, "workspace_dir", ""),
        "cmd_timeout_s": getattr(config, "cmd_timeout_s", 0),
        "cmd_async_ceiling_s": getattr(config, "cmd_async_ceiling_s", 0),
        "bg_job_max_age_s": getattr(config, "bg_job_max_age_s", 0),
        "dashboard_port": getattr(config, "dashboard_port", 0),
        "voice_port": getattr(config, "voice_port", 0),
        "protected_patterns": getattr(config, "protected_patterns", []),
    }
    try:
        return ResolvedConfigBoundary.model_validate(data)
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid resolved config: {_format_validation_error(error)}"
        ) from error


# --- Dashboard POST payloads --------------------------------------------


class DashboardPayloadError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


class DashboardChatPost(_StrictBoundaryModel):
    message: str = Field(min_length=1, max_length=2000)


class DashboardResetPost(_StrictBoundaryModel):
    mode: Literal["rebirth", "full"] = "rebirth"


class DashboardConfigPost(_StrictBoundaryModel):
    settings: dict[str, Any] = Field(min_length=1)
    apply: bool = True


class DashboardLlmTestPost(_StrictBoundaryModel):
    url: str | None = Field(default=None, max_length=2000)

    @field_validator("url")
    @classmethod
    def _empty_url_is_none(cls, value: str | None) -> str | None:
        return value or None


class DashboardChatHoldPost(_StrictBoundaryModel):
    held: bool = False


class DashboardSelfGuidePost(_StrictBoundaryModel):
    content: str = Field(default="", max_length=20000)


class DashboardGitCheckpointPost(_StrictBoundaryModel):
    label: str = Field(default="", max_length=80)


class DashboardGitRestorePost(_StrictBoundaryModel):
    tag: str = Field(min_length=1, max_length=120)


class DashboardSelfEditApplyPost(_StrictBoundaryModel):
    id: str = Field(min_length=1, max_length=80)


class DashboardSelfEditRejectPost(_StrictBoundaryModel):
    id: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=200)


_DASHBOARD_POST_MODELS: dict[str, tuple[type[_StrictBoundaryModel], bool]] = {
    "/api/chat": (DashboardChatPost, False),
    "/api/control/reset": (DashboardResetPost, True),
    "/api/config": (DashboardConfigPost, False),
    "/api/llm/test": (DashboardLlmTestPost, True),
    "/api/chat_hold": (DashboardChatHoldPost, True),
    "/api/self_guide": (DashboardSelfGuidePost, True),
    "/api/git/checkpoint": (DashboardGitCheckpointPost, True),
    "/api/git/restore": (DashboardGitRestorePost, False),
    "/api/selfedit/apply": (DashboardSelfEditApplyPost, False),
    "/api/selfedit/reject": (DashboardSelfEditRejectPost, False),
}


def _decode_json_mapping(raw: bytes | str | Mapping[str, Any] | None, *, allow_empty: bool) -> dict[str, Any]:
    if raw is None or raw == b"" or raw == "":
        if allow_empty:
            return {}
        raise DashboardPayloadError("bad body")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise DashboardPayloadError("invalid json") from error
    if not isinstance(data, dict):
        raise DashboardPayloadError("json body must be an object")
    return data


def validate_dashboard_post_payload(path: str, raw: bytes | str | Mapping[str, Any] | None) -> _StrictBoundaryModel:
    model_info = _DASHBOARD_POST_MODELS.get(path)
    if model_info is None:
        raise DashboardPayloadError(f"unsupported POST path {path!r}", status=404)
    model, allow_empty = model_info
    data = _decode_json_mapping(raw, allow_empty=allow_empty)
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise DashboardPayloadError(
            f"invalid payload: {_format_validation_error(error)}"
        ) from error


# --- Durable JSON state records -----------------------------------------


class ObservationRecord(_CompatStateRecord):
    ts: str | None = None
    tick: int | Literal["compaction"] | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    success: bool | None = None
    output: Any = None
    duration_s: float | None = Field(default=None, ge=0)
    fail_kind: str | None = None

    @field_validator("tick")
    @classmethod
    def _tick_nonnegative(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, int) and value < 0:
            raise ValueError("tick must be nonnegative")
        return value


class ChatReplyRecord(_CompatStateRecord):
    ts: str
    text: str = Field(min_length=1, max_length=2000)
    spoken: bool = False
    tick: int | None = Field(default=None, ge=0)
    status: str | None = None


class JobRecord(_CompatStateRecord):
    name: str = Field(min_length=1)
    status: Literal["running", "completed", "failed", "timed_out", "reaped"]
    pid: int | None = Field(default=None, ge=0)
    cmd: str | None = None
    intent: str | None = None
    started: str | None = None
    started_ts: float | None = Field(default=None, ge=0)
    kind: str | None = None
    mode: str | None = None
    output_path: str | None = None
    exit_path: str | None = None
    exit_code: int | None = None
    notified: bool = False
    waited: bool = False


def _dump_record(record: BaseModel) -> dict[str, Any]:
    return record.model_dump(exclude_none=True, exclude_defaults=True)


def validate_observation_record(entry: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _dump_record(ObservationRecord.model_validate(entry))
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid observation record: {_format_validation_error(error)}"
        ) from error


def validate_chat_reply_record(entry: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _dump_record(ChatReplyRecord.model_validate(entry))
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid chat reply record: {_format_validation_error(error)}"
        ) from error


def validate_job_records(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise BoundaryValidationError("invalid jobs ledger: expected a list")
    out = []
    try:
        for entry in entries:
            out.append(_dump_record(JobRecord.model_validate(entry)))
    except ValidationError as error:
        raise BoundaryValidationError(
            f"invalid jobs ledger: {_format_validation_error(error)}"
        ) from error
    return out


def boundary_schema_bundle() -> dict[str, Any]:
    """JSON-schema bundle used by docs/boundary-schemas.json."""

    return {
        "generated_by": "scripts/check_boundary_schemas.py --write",
        "config": {
            "document": ConfigDocument.model_json_schema(),
            "env": EidosEnvOverrides.model_json_schema(),
            "resolved": ResolvedConfigBoundary.model_json_schema(),
        },
        "dashboard_posts": {
            path: model.model_json_schema()
            for path, (model, _allow_empty) in sorted(_DASHBOARD_POST_MODELS.items())
        },
        "state_records": {
            "observation": ObservationRecord.model_json_schema(),
            "chat_reply": ChatReplyRecord.model_json_schema(),
            "job": JobRecord.model_json_schema(),
        },
    }
