"""Configuration management for the Assay verification runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILENAME = ".assay.yaml"
DEFAULT_SESSIONS_DIR = Path("./.assay/sessions")
DEFAULT_KEYS_DIR = Path("./.assay/keys")
DEFAULT_REDACTION_FIELDS: list[str] = [
    "Authorization",
    "Cookie",
    "Set-Cookie",
    "X-Api-Key",
    "password",
    "secret",
    "token",
    "credit_card",
    "cvv",
    "number",
]


class StorageSettings(BaseModel):
    """Filesystem paths for Assay sessions and cryptographic keys."""

    sessions_dir: Path = Field(
        default=DEFAULT_SESSIONS_DIR,
        description="Directory where recorded workload sessions are stored.",
    )
    keys_dir: Path = Field(
        default=DEFAULT_KEYS_DIR,
        description="Directory where Ed25519 signing keys are stored.",
    )


class NetworkSettings(BaseModel):
    """Networking and reverse‑proxy configuration parameters."""

    timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="HTTP connection and request timeout in seconds.",
    )
    proxy_host: str = Field(
        default="127.0.0.1",
        min_length=1,
        description="Host interface for the recording reverse‑proxy server.",
    )
    proxy_port: int = Field(
        default=9000,
        ge=1,
        le=65535,
        description="TCP port for the recording reverse‑proxy server.",
    )
    # New dual‑routing settings
    backend_target: str = Field(
        default="http://localhost:5000",
        description="Upstream URL for backend API traffic.",
    )
    frontend_target: Optional[str] = Field(
        default="http://localhost:5173",
        description="Upstream URL for frontend static assets/UI traffic. Optional.",
    )
    api_prefixes: List[str] = Field(
        default_factory=lambda: ["/api", "/auth", "/graphql", "/health"],
        description="Path prefixes that identify API requests.",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum number of retries for transient HTTP replay failures.",
    )
    backoff_factor: float = Field(
        default=0.5,
        ge=0.0,
        description="Base exponential backoff factor in seconds between retries.",
    )

class RedactionSettings(BaseModel):
    """Sensitive field redaction rules applied during recording."""

    fields: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REDACTION_FIELDS),
        description="Header and JSON body field names to redact from recordings.",
    )


class AssaySettings(BaseSettings):
    """Root configuration schema for Assay, loaded from YAML and environment."""

    model_config = SettingsConfigDict(
        env_prefix="ASSAY_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    storage: StorageSettings = Field(default_factory=StorageSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)

    @classmethod
    def from_yaml(cls, config_path: Path | None = None) -> AssaySettings:
        """Load settings from a YAML file, falling back to defaults if absent.

        Args:
            config_path: Path to the YAML configuration file. Defaults to
                ``./.assay.yaml`` in the current working directory.

        Returns:
            Validated ``AssaySettings`` instance.
        """
        target = config_path or Path(DEFAULT_CONFIG_FILENAME)
        if not target.exists():
            return cls()

        raw_text = target.read_text(encoding="utf-8")
        parsed: Any = yaml.safe_load(raw_text)
        if parsed is None:
            return cls()
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Expected mapping at root of {target}, got {type(parsed).__name__}."
            )

        return cls.model_validate(parsed)

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize settings into a plain YAML-safe dictionary."""
        return self.model_dump(mode="json")


def load_settings(config_path: Path | None = None) -> AssaySettings:
    """Load Assay configuration from disk or return default settings.

    Args:
        config_path: Optional explicit path to a ``.assay.yaml`` file.

    Returns:
        Validated ``AssaySettings`` instance.
    """
    return AssaySettings.from_yaml(config_path=config_path)

# Ensure Pydantic models are fully built after dynamic field additions
AssaySettings.model_rebuild()


def write_default_config(
    config_path: Path | None = None,
    settings: AssaySettings | None = None,
) -> Path:
    """Write an Assay configuration file in YAML format.

    Args:
        config_path: Destination file path. Defaults to ``./.assay.yaml``.
        settings: Optional settings instance to serialize; uses defaults if omitted.

    Returns:
        Path to the written configuration file.
    """
    target = config_path or Path(DEFAULT_CONFIG_FILENAME)
    active_settings = settings or AssaySettings()
    target.parent.mkdir(parents=True, exist_ok=True)
    yaml_content = yaml.safe_dump(
        active_settings.to_yaml_dict(),
        sort_keys=False,
        default_flow_style=False,
    )
    target.write_text(yaml_content, encoding="utf-8")
    return target
