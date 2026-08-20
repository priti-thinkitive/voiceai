"""Typed application settings, loaded from `.env`.

Bootstrap step 2. VoiceAI is B2B infrastructure — the only "secrets" that
exist at this stage are the vendor (Retell) API key and whatever salts/keys
protect platform API keys later. Settings.assert_production_secrets() is the
fail-fast gate that refuses to boot a production deployment with weak/missing
config; call it once at startup (see main.py).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ before anything else, so third-party libraries
# that read the process env directly (not just pydantic-settings) also see
# these values. pydantic-settings independently re-reads .env below; the two
# are redundant on purpose — see eCareVoiceAI's config.py for the same
# pattern and rationale.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ENV: Literal["development", "production", "test"] = "development"

    # Picked to avoid colliding with eCareVoiceAI's backend (port 8100) when
    # both run locally at once.
    PORT: int = 8200
    BASE_URL: str = "http://localhost:8200"

    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "VoiceAI"

    # CORS — platforms integrate server-to-server, so this stays empty/strict
    # by default. No dashboard/browser client exists yet at bootstrap time.
    CORS_ALLOWED_ORIGINS: str = ""

    # Host allowlist for TrustedHostMiddleware. "*" disables the check —
    # fine for local dev, must be a real allowlist in production.
    ALLOWED_HOSTS: str = "*"

    # Vendor adapter — Retell today, swappable later. Held only in our
    # backend config; never returned to a calling platform.
    RETELL_API_KEY: str = ""
    RETELL_API_BASE: str = "https://api.retellai.com"

    # Internal admin credential — a single shared secret authenticating
    # VoiceAI's OWN team (not a regular platform) to admin-only endpoints,
    # e.g. POST /platforms (see app/routers/platform.py). Deliberately one
    # shared value, not a per-admin-account system with its own login/DB
    # table — this is the smallest correct building block for "only our own
    # team can call this," not a multi-user admin identity system (that is a
    # separate, later, bigger initiative — see get_admin_caller's own
    # docstring in app/deps.py for the full reasoning). Compared via
    # hmac.compare_digest, never `==` (see get_admin_caller). Unset is
    # allowed in dev/test (see get_admin_caller's dev-permissive-vs-fail-
    # closed handling); required in production — see
    # assert_production_secrets() below.
    ADMIN_API_KEY: str = ""

    # Re-hosting storage for call recordings/transcripts (Phase 1 item 15 —
    # "re-host, don't pass through": we never hand Platform X a vendor-hosted
    # URL). Real AWS credentials not yet provisioned — placeholders only, so
    # the config shape exists and code can be built/reviewed against it
    # before real credentials are added (same pattern RETELL_API_KEY started
    # from). Empty AWS_S3_BUCKET is the signal this isn't configured yet.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # Docs are dev-only per the Swagger standard.
    @property
    def docs_enabled(self) -> bool:
        return self.ENV == "development"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        hosts = [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]
        return hosts or ["*"]

    def assert_production_secrets(self) -> None:
        """Fail-fast in production if config is weak, default, or unsafe.

        Refuses to boot with ENV=production when: no Retell key configured
        (the vendor adapter has nothing to talk to, AND — per
        app/routers/webhooks.py's `_verify_signature` — unsigned Retell
        webhooks would be forgeable, since RETELL_API_KEY doubles as the
        webhook-signature-verification secret, not a separate
        RETELL_WEBHOOK_SECRET; see that module's docstring for the full,
        sourced reasoning), no admin API key configured (see
        app/deps.py's `get_admin_caller`), wildcard CORS, or a wildcard host
        allowlist. Extend this as new secret-bearing settings are added (e.g. the
        API-key hashing pepper, once introduced).
        """
        if self.ENV != "production":
            return
        problems: list[str] = []
        if not self.RETELL_API_KEY:
            problems.append(
                "RETELL_API_KEY must be set in production (required for both "
                "outbound Retell API calls and inbound Retell webhook "
                "signature verification)"
            )
        if not self.ADMIN_API_KEY:
            problems.append(
                "ADMIN_API_KEY must be set in production (required to authenticate "
                "VoiceAI's own team to admin-only endpoints, e.g. POST /platforms — "
                "without it, platform onboarding has no HTTP path at all in production, "
                "only the emergency scripts/seed_platform.py fallback)"
            )
        if not self.AWS_S3_BUCKET or not self.AWS_ACCESS_KEY_ID or not self.AWS_SECRET_ACCESS_KEY:
            problems.append(
                "AWS_S3_BUCKET/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY must all be set in "
                "production (re-hosted recordings/transcripts have nowhere to be stored "
                "without them)"
            )
        if "*" in self.cors_origins_list:
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*' in production")
        if not self.CORS_ALLOWED_ORIGINS:
            problems.append("CORS_ALLOWED_ORIGINS must be explicitly set in production")
        if self.allowed_hosts_list == ["*"]:
            problems.append("ALLOWED_HOSTS must not be '*' in production")
        if problems:
            raise RuntimeError("Invalid production config: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Construct the Settings singleton and enforce production fail-fast checks."""
    settings = Settings()
    settings.assert_production_secrets()
    return settings
