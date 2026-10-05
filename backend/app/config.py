from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787

    # Prefer setting DATABASE_URL in .env; this is only a local-dev fallback.
    database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:5432/hermesdashboard"

    # Used in Hermes / Apple Intelligence prompts.
    owner_name: str = "you"
    agent_name: str = "Gladys"

    hermes_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_key: str = "change-me-local-dev"
    hermes_model: str = "hermes-agent"
    # Briefing, Ask, and email triage go to the siri CLI. Hermes is the fallback.
    apple_intelligence: bool = True
    siri_bin: str = ""
    siri_shortcut: str = "Ask Apple Intelligence (Private Cloud Compute)"
    # Repo-root relative (or absolute) path to optional prompt rules.
    hermes_rules_path: str = "hermes-rules.local"

    # Empty = auto (DB preference → system IANA → UTC).
    dashboard_timezone: str = ""

    obsidian_mcp_url: str = "http://127.0.0.1:27123/mcp/"
    obsidian_mcp_token: str = ""
    # Absolute filesystem path to the vault (dataview file scans). Empty disables.
    obsidian_vault_path: str = ""
    obsidian_daily_folder: str = (
        "📁 500 📒 Notes/📁 510 🧹 Maintenance/📁 511 🔍 Reviews/1. Daily"
    )
    obsidian_daily_format: str = "D-YYYY-MM-DD"
    obsidian_template_path: str = (
        "📁 300 💡 Resources/📁 330 㡯 Templates/Daily Template.md"
    )
    obsidian_vault_name: str = "My Vault"
    obsidian_agent_receipt_folder: str = (
        "📁 500 📒 Notes/📁 520 🗒 Zettelkasten/521 📝 Rough Notes/🤖 Gladys"
    )

    omnifocus_mcp_command: str = (
        "/Users/antonio/Dropbox/Code/Python/mcp/omnifocus-mcp/.venv/bin/omnifocus-mcp"
    )
    omnifocus_on_deck_perspective: str = "On Deck"
    omnifocus_tomorrow_perspective: str = "Tomorrow > 5 minutes"
    omnifocus_agent_enabled: bool = True
    omnifocus_agent_tag: str = "🤖 Gladys"
    omnifocus_agent_running_tag: str = "gladys-running"
    omnifocus_agent_done_tag: str = "gladys-done"
    omnifocus_agent_blocked_tag: str = "gladys-blocked"
    omnifocus_agent_failed_tag: str = "gladys-failed"
    omnifocus_agent_poll_seconds: int = 900
    # 0 = wait as long as the Hermes gateway will. Gladys jobs are overnight work.
    omnifocus_agent_timeout_seconds: int = 0
    omnifocus_review_tag: str = "🔎 Review"

    fantastical_mcp_command: str = (
        "/Applications/Fantastical.app/Contents/Helpers/"
        "FantasticalMCP.app/Contents/MacOS/FantasticalMCP"
    )

    kikodo_crm_mcp_command: str = "/Users/antonio/micromamba/envs/kikodo-crm/bin/python"
    kikodo_crm_mcp_args: str = (
        "/Users/antonio/Dropbox/Code/Python/Rocket/git/kikodo-crm/crm_mcp_server.py"
    )
    kikodo_crm_mcp_cwd: str = "/Users/antonio/Dropbox/Code/Python/Rocket/git/kikodo-crm"

    # Pushover Message API: https://pushover.net/api
    # token = application API token; user = user/group key.
    pushover_user_key: str = ""
    pushover_api_key: str = ""
    briefing_note_path: str = (
        "/Users/antonio/Library/Mobile Documents/com~apple~CloudDocs/"
        "Hermes Dashboard/Hermes Briefing.md"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
