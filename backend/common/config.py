"""
Configuration and secret handling.

Loaded once at startup, validated eagerly, and immutable thereafter -- a
missing Agora credential should fail the process on line one, not fifteen
minutes into a judged demo.

Secrets are wrapped in :class:`Secret`, whose ``repr``/``str`` redact. That
is not decoration: the structured logger serialises arbitrary context
dictionaries, and a bare string secret placed in one would be written to
disk. Making the redaction a property of the *type* means it cannot be
forgotten at a call site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from backend.common.errors import ConfigError

# --------------------------------------------------------------------------
# Secret
# --------------------------------------------------------------------------


class Secret:
    """A string that refuses to render itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The only way to read the underlying value. Grep-able on purpose."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret('***redacted***')" if self._value else "Secret(empty)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            # Constant-time-ish comparison is overkill here (no remote
            # attacker times these), but never leak by accident.
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


# --------------------------------------------------------------------------
# Env helpers
# --------------------------------------------------------------------------


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    raw = env.get(key, default)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _require(env: Mapping[str, str], key: str) -> str:
    value = _get(env, key)
    if not value:
        raise ConfigError(f"required environment variable {key} is not set", variable=key)
    return value


def _get_int(env: Mapping[str, str], key: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer", variable=key, value=raw) from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}", variable=key, value=value)
    return value


def _get_float(env: Mapping[str, str], key: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number", variable=key, value=raw) from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}", variable=key, value=value)
    return value


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean", variable=key, value=raw)


# --------------------------------------------------------------------------
# Config sections
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgoraConfig:
    """Agora Conversational AI Engine settings.

    ``customer_id``/``customer_secret`` are the **Basic Auth** pair from
    Console → Developer Toolkit → RESTful API. They are *not* the App ID and
    App Certificate; using the latter for REST auth is the single most
    common wiring mistake here, so the distinction is encoded in the names
    and checked below.
    """

    app_id: str
    channel_name: str
    customer_id: Secret
    customer_secret: Secret
    app_certificate: Secret = Secret("")
    base_url: str = "https://api.agora.io"
    request_timeout_seconds: float = 8.0
    speak_max_bytes: int = 512
    enable_metrics: bool = True
    agent_uid: str = "9000"
    token_ttl_seconds: int = 3600

    @property
    def is_authenticated(self) -> bool:
        return bool(self.customer_id) and bool(self.customer_secret)

    def require_auth(self) -> None:
        if not self.is_authenticated:
            raise ConfigError(
                "Agora REST calls need AGORA_CUSTOMER_ID and AGORA_CUSTOMER_SECRET "
                "(Console → Developer Toolkit → RESTful API), not the App Certificate",
                variables=["AGORA_CUSTOMER_ID", "AGORA_CUSTOMER_SECRET"],
            )

    @property
    def can_issue_client_tokens(self) -> bool:
        return bool(self.app_id) and bool(self.app_certificate)

    def require_token_issuer(self) -> None:
        if not self.can_issue_client_tokens:
            raise ConfigError(
                "Agora RTC/RTM token issuance needs AGORA_APP_ID and AGORA_APP_CERTIFICATE",
                variables=["AGORA_APP_ID", "AGORA_APP_CERTIFICATE"],
            )


@dataclass(frozen=True)
class LlmConfig:
    """Extraction provider settings.

    ``provider`` selects the implementation; ``deterministic`` is the offline
    provider used by tests and by the demo fallback, and needs no key.
    """

    provider: str = "deterministic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: Secret = Secret("")
    request_timeout_seconds: float = 6.0
    max_attempts: int = 2
    temperature: float = 0.0
    vision_model: Optional[str] = None

    @property
    def supports_vision(self) -> bool:
        """Phase 2 gate. Flagged rather than assumed: the multimodal
        extension must not silently substitute a different provider."""
        return bool(self.vision_model)


@dataclass(frozen=True)
class GovernorConfig:
    """Intervention pacing.

    ``rate_limit_seconds`` is a frozen architectural constant (SSOT §25
    decision #9), not a tuning knob. It is configurable only so tests can
    compress it; production/demo runs must leave it at 45.
    """

    rate_limit_seconds: float = 45.0
    queue_max_age_seconds: float = 180.0
    max_queue_depth: int = 16


@dataclass(frozen=True)
class PipelineConfig:
    ingest_queue_max_depth: int = 256
    worker_count: int = 1
    confirmation_window_seconds: float = 120.0
    stale_after_seconds: float = 300.0


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    ingest_token: Secret = Secret("")
    max_body_bytes: int = 1 * 1024 * 1024
    max_upload_bytes: int = 8 * 1024 * 1024
    ingest_rate_limit_per_minute: int = 600
    cors_allow_origins: tuple[str, ...] = ()

    @property
    def auth_enabled(self) -> bool:
        return bool(self.ingest_token)


@dataclass(frozen=True)
class AppConfig:
    agora: AgoraConfig
    llm: LlmConfig
    governor: GovernorConfig
    pipeline: PipelineConfig
    api: ApiConfig
    database_path: Path
    incident_id: str
    log_level: str = "INFO"
    log_file: Optional[Path] = None
    environment: str = "local"

    def warnings(self) -> tuple[str, ...]:
        """Non-fatal misconfigurations worth surfacing loudly at startup.

        Deliberately not exceptions: a text-only pipeline run needs neither
        Agora credentials nor an LLM key, and refusing to boot without them
        would make the offline development path impossible.
        """
        issues: list[str] = []
        if not self.agora.is_authenticated:
            issues.append(
                "Agora Customer ID/Secret absent -- /join and /speak will fail. "
                "Live voice is disabled; text-only pipeline still works."
            )
        if not self.agora.can_issue_client_tokens:
            issues.append(
                "Agora App ID/App Certificate absent -- browser RTC/RTM voice sessions are disabled."
            )
        if self.llm.provider != "deterministic" and not self.llm.api_key:
            issues.append(
                f"LLM provider '{self.llm.provider}' selected but no API key set -- "
                "extraction will fall back to the deterministic provider."
            )
        if not self.api.auth_enabled:
            issues.append(
                "AEGIS_INGEST_TOKEN is not set: ingestion endpoints are unauthenticated. "
                "Acceptable on localhost only -- an unauthenticated endpoint that accepts "
                "a confirmation claim is a path to unauthorised action."
            )
        if self.governor.rate_limit_seconds != GovernorConfig.rate_limit_seconds:
            issues.append(
                f"Governor rate limit overridden to {self.governor.rate_limit_seconds}s; "
                "the frozen architectural constant is 45s (SSOT §25 decision #9)."
            )
        return tuple(issues)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader.

    Hand-rolled rather than depending on python-dotenv: it is twenty lines,
    it removes a dependency from the critical startup path, and it lets the
    parse errors be AEGIS errors with line numbers.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"malformed .env line {lineno}: expected KEY=VALUE", path=str(path))
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    dotenv_path: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> AppConfig:
    """Build the immutable application config.

    Process environment wins over ``.env`` so a shell override always takes
    effect -- the usual precedence, stated explicitly because getting it
    backwards makes debugging a demo failure miserable.
    """
    root = project_root or Path(__file__).resolve().parents[2]
    dotenv = _load_dotenv(dotenv_path or (root / ".env"))
    process_env = dict(os.environ if env is None else env)
    merged: dict[str, str] = {**dotenv, **process_env}

    agora = AgoraConfig(
        app_id=_get(merged, "AGORA_APP_ID") or "",
        channel_name=_get(merged, "AGORA_CHANNEL_NAME") or "aegis-incident",
        customer_id=Secret(_get(merged, "AGORA_CUSTOMER_ID") or ""),
        customer_secret=Secret(_get(merged, "AGORA_CUSTOMER_SECRET") or ""),
        app_certificate=Secret(_get(merged, "AGORA_APP_CERTIFICATE") or ""),
        base_url=_get(merged, "AGORA_BASE_URL") or "https://api.agora.io",
        request_timeout_seconds=_get_float(merged, "AGORA_TIMEOUT_SECONDS", 8.0, minimum=0.5),
        enable_metrics=_get_bool(merged, "AGENT_METRICS_ENABLED", True),
        agent_uid=_get(merged, "AGORA_AGENT_UID") or "9000",
        token_ttl_seconds=_get_int(merged, "AGORA_TOKEN_TTL_SECONDS", 3600, minimum=60),
    )

    llm = LlmConfig(
        provider=(_get(merged, "LLM_PROVIDER") or "deterministic").lower(),
        base_url=_get(merged, "LLM_BASE_URL") or "https://api.openai.com/v1",
        model=_get(merged, "LLM_MODEL") or "gpt-4o-mini",
        api_key=Secret(_get(merged, "LLM_API_KEY") or ""),
        request_timeout_seconds=_get_float(merged, "LLM_TIMEOUT_SECONDS", 6.0, minimum=0.5),
        max_attempts=_get_int(merged, "LLM_MAX_ATTEMPTS", 2, minimum=1),
        temperature=_get_float(merged, "LLM_TEMPERATURE", 0.0, minimum=0.0),
        vision_model=_get(merged, "LLM_VISION_MODEL"),
    )

    governor = GovernorConfig(
        rate_limit_seconds=_get_float(merged, "GOVERNOR_RATE_LIMIT_SECONDS", 45.0, minimum=0.0),
        queue_max_age_seconds=_get_float(merged, "GOVERNOR_QUEUE_MAX_AGE_SECONDS", 180.0, minimum=0.0),
        max_queue_depth=_get_int(merged, "GOVERNOR_MAX_QUEUE_DEPTH", 16, minimum=1),
    )

    pipeline = PipelineConfig(
        ingest_queue_max_depth=_get_int(merged, "PIPELINE_QUEUE_DEPTH", 256, minimum=1),
        worker_count=_get_int(merged, "PIPELINE_WORKERS", 1, minimum=1),
        confirmation_window_seconds=_get_float(merged, "CONFIRMATION_WINDOW_SECONDS", 120.0, minimum=1.0),
        stale_after_seconds=_get_float(merged, "HYPOTHESIS_STALE_AFTER_SECONDS", 300.0, minimum=1.0),
    )

    cors_raw = _get(merged, "AEGIS_CORS_ORIGINS") or ""
    api = ApiConfig(
        host=_get(merged, "AEGIS_HOST") or "127.0.0.1",
        port=_get_int(merged, "AEGIS_PORT", 8080, minimum=1),
        ingest_token=Secret(_get(merged, "AEGIS_INGEST_TOKEN") or ""),
        max_body_bytes=_get_int(merged, "AEGIS_MAX_BODY_BYTES", 1024 * 1024, minimum=1024),
        max_upload_bytes=_get_int(merged, "AEGIS_MAX_UPLOAD_BYTES", 8 * 1024 * 1024, minimum=1024),
        ingest_rate_limit_per_minute=_get_int(merged, "AEGIS_INGEST_RPM", 600, minimum=1),
        cors_allow_origins=tuple(o.strip() for o in cors_raw.split(",") if o.strip()),
    )

    log_file_raw = _get(merged, "AEGIS_LOG_FILE")
    database_raw = _get(merged, "AEGIS_DB_PATH") or str(root / "data" / "incident.db")

    config = AppConfig(
        agora=agora,
        llm=llm,
        governor=governor,
        pipeline=pipeline,
        api=api,
        database_path=Path(database_raw),
        incident_id=_get(merged, "AEGIS_INCIDENT_ID") or "incident-local",
        log_level=(_get(merged, "AEGIS_LOG_LEVEL") or "INFO").upper(),
        log_file=Path(log_file_raw) if log_file_raw else None,
        environment=_get(merged, "AEGIS_ENV") or "local",
    )

    if config.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("AEGIS_LOG_LEVEL must be a standard logging level", value=config.log_level)
    return config


SENSITIVE_KEY_HINTS: frozenset[str] = frozenset(
    {"secret", "token", "api_key", "apikey", "password", "authorization", "auth", "credential"}
)

_REDACTED = "***redacted***"
_MAX_REDACT_DEPTH = 6


def redact(value: Any, _depth: int = 0) -> Any:
    """Best-effort redaction for values headed into a log record.

    Recurses into mappings *via* :func:`redact_mapping` so that key-name
    matching applies at every level -- a secret nested two dicts deep is
    still a secret. Depth-bounded so a cyclic or pathological structure can
    never hang the logger.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return "***truncated***"
    if isinstance(value, Secret):
        return _REDACTED
    if isinstance(value, Mapping):
        return redact_mapping(value, _depth=_depth + 1)
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(repr(redact(item, _depth + 1)) for item in value)
    return value


def redact_mapping(data: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Redact by value *type* and by key name, recursively.

    Key-name matching catches the case where a raw string secret was passed
    in from a boundary that never wrapped it in :class:`Secret`.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return {"_": "***truncated***"}
    out: dict[str, Any] = {}
    for key, value in data.items():
        key_str = str(key)
        lowered = key_str.lower()
        if any(hint in lowered for hint in SENSITIVE_KEY_HINTS):
            out[key_str] = _REDACTED
        else:
            out[key_str] = redact(value, _depth + 1)
    return out


def iter_config_summary(config: AppConfig) -> Iterable[tuple[str, Any]]:
    """Startup banner content -- safe to log, by construction."""
    yield "environment", config.environment
    yield "incident_id", config.incident_id
    yield "database_path", str(config.database_path)
    yield "agora_app_id_set", bool(config.agora.app_id)
    yield "agora_authenticated", config.agora.is_authenticated
    yield "agora_channel", config.agora.channel_name
    yield "llm_provider", config.llm.provider
    yield "llm_model", config.llm.model
    yield "llm_vision_enabled", config.llm.supports_vision
    yield "governor_rate_limit_seconds", config.governor.rate_limit_seconds
    yield "api_auth_enabled", config.api.auth_enabled
    yield "api_bind", f"{config.api.host}:{config.api.port}"
