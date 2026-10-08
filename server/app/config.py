from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes")


@dataclass
class Entra:
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    allowed_group_id: str = ""


@dataclass
class Confluence:
    base_url: str = ""  # e.g. https://kasasa.atlassian.net/wiki  or  https://confluence.corp.example.com
    email: str = ""
    api_token: str = ""
    pat: str = ""  # Data Center personal access token (instead of email + API token)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and (self.pat or (self.email and self.api_token)))


@dataclass
class Claude:
    api_key: str = ""
    model: str = "claude-sonnet-5-5"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class AzureOpenAI:
    endpoint: str = ""  # https://<resource>.openai.azure.com
    api_key: str = ""
    deployment: str = ""
    api_version: str = "2024-10-21"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.deployment)


@dataclass
class Settings:
    demo: bool = False
    base_url: str = "http://localhost:8080"
    org_name: str = "Kasasa"
    data_dir: str = "./data"
    web_dir: str = "../web/dist"
    session_hours: int = 8
    max_upload_mb: int = 25
    default_ai: str = "claude"
    entra: Entra = field(default_factory=Entra)
    confluence: Confluence = field(default_factory=Confluence)
    claude: Claude = field(default_factory=Claude)
    azure: AzureOpenAI = field(default_factory=AzureOpenAI)

    def ai_providers(self) -> list[dict]:
        out = []
        if self.demo:
            out.append({"key": "demo", "name": "Demo AI (sample matching, no data leaves the server)"})
        if self.claude.configured:
            out.append({"key": "claude", "name": f"Claude ({self.claude.model})"})
        if self.azure.configured:
            out.append({"key": "azure", "name": f"Azure OpenAI ({self.azure.deployment})"})
        return out


def load() -> Settings:
    e = os.environ.get
    s = Settings(
        demo=_bool(e("DEMO_MODE")),
        base_url=(e("BASE_URL") or "http://localhost:8080").rstrip("/"),
        org_name=e("ORG_NAME") or "Kasasa",
        data_dir=e("DATA_DIR") or "./data",
        web_dir=e("WEB_DIR") or "../web/dist",
        session_hours=int(e("SESSION_HOURS") or 8),
        max_upload_mb=int(e("MAX_UPLOAD_MB") or 25),
        default_ai=(e("DEFAULT_AI_PROVIDER") or "claude").lower(),
        entra=Entra(e("ENTRA_TENANT_ID") or "", e("ENTRA_CLIENT_ID") or "", e("ENTRA_CLIENT_SECRET") or "", e("ENTRA_ALLOWED_GROUP_ID") or ""),
        confluence=Confluence(e("CONFLUENCE_BASE_URL") or "", e("CONFLUENCE_EMAIL") or "", e("CONFLUENCE_API_TOKEN") or "", e("CONFLUENCE_PAT") or ""),
        claude=Claude(e("ANTHROPIC_API_KEY") or "", e("ANTHROPIC_MODEL") or "claude-sonnet-5-5"),
        azure=AzureOpenAI(e("AZURE_OPENAI_ENDPOINT", "").rstrip("/"), e("AZURE_OPENAI_API_KEY") or "", e("AZURE_OPENAI_DEPLOYMENT") or "", e("AZURE_OPENAI_API_VERSION") or "2024-10-21"),
    )
    return s


settings = load()
