"""Runtime settings, all from environment variables.

On OpenShift they come from:
  * the connection Secret ``aap-migration-data``  -> AWS_* (S3 workspace)
  * the ConfigMap ``migration-ai-config``         -> LLM_*, MIGRATION_*, MODEL_*
  * the Secret ``aap-bridge-env`` mounted as file -> TOOLKIT_ENV_FILE
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path

WORKSPACE_FOLDERS = ("exports", "xformed", "reports", "logs", "database", "schemas")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


SERVICE_CA = "/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"


def _env_verify(name: str, endpoint_var: str = "") -> bool | str:
    """True / False, or the path of a CA bundle.

    With "true" (the default) and an in-cluster endpoint (*.svc, e.g. ODF / MCG
    https://s3.openshift-storage.svc), the certificate is signed by the OpenShift service CA,
    which is not in the system bundle: the service CA mounted in every pod is used instead."""
    val = (os.environ.get(name) or "").strip()
    if val.startswith("/"):
        return val
    verify = _env_bool(name, True)
    if verify and endpoint_var:
        host = urlparse(os.environ.get(endpoint_var) or "").hostname or ""
        if (host.endswith(".svc") or host.endswith(".svc.cluster.local")) and os.path.isfile(SERVICE_CA):
            return SERVICE_CA
    return verify


@dataclass
class S3Settings:
    endpoint: str = field(default_factory=lambda: _env("AWS_S3_ENDPOINT"))
    bucket: str = field(default_factory=lambda: _env("AWS_S3_BUCKET", "aap-migration-data"))
    access_key: str = field(default_factory=lambda: _env("AWS_ACCESS_KEY_ID"))
    secret_key: str = field(default_factory=lambda: _env("AWS_SECRET_ACCESS_KEY"))
    region: str = field(default_factory=lambda: _env("AWS_DEFAULT_REGION", "us-east-1"))
    verify_tls: bool | str = field(default_factory=lambda: _env_verify("AWS_S3_VERIFY_TLS", "AWS_S3_ENDPOINT"))

    @property
    def configured(self) -> bool:
        return bool(self.bucket and self.access_key and self.secret_key)


@dataclass
class LLMSettings:
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "not-needed"))
    temperature: float = field(default_factory=lambda: float(_env("LLM_TEMPERATURE", "0.1") or 0.1))
    max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "2048") or 2048))
    timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT", "180") or 180))
    verify_tls: bool = field(default_factory=lambda: _env_bool("LLM_VERIFY_TLS", True))
    extra_body: dict = field(default_factory=lambda: json.loads(_env("LLM_EXTRA_BODY") or "{}"))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class Settings:
    work_dir: Path = field(default_factory=lambda: Path(_env("WORK_DIR", "/work")))
    migrations_prefix: str = field(default_factory=lambda: _env("MIGRATIONS_PREFIX", "migrations").strip("/"))
    models_prefix: str = field(
        default_factory=lambda: _env("MODELS_PREFIX", "models/aap-migration-risk").strip("/")
    )
    toolkit_config: str = field(
        default_factory=lambda: _env("AAP_BRIDGE_CONFIG", "/opt/aap-bridge/config/config.yaml")
    )
    toolkit_env_file: str = field(default_factory=lambda: _env("TOOLKIT_ENV_FILE", "/etc/aap-bridge/.env"))
    toolkit_dir: str = field(default_factory=lambda: _env("TOOLKIT_DIR", "/opt/aap-bridge"))
    s3: S3Settings = field(default_factory=S3Settings)
    llm: LLMSettings = field(default_factory=LLMSettings)

    def migration_prefix(self, migration_id: str) -> str:
        return f"{self.migrations_prefix}/{migration_id}"


def load_settings() -> Settings:
    return Settings()
