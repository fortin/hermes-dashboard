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

    database_url: str = (
        "postgresql+asyncpg://antonio@127.0.0.1:5432/hermesdashboard"
    )

    hermes_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_key: str = "change-me-local-dev"
    hermes_model: str = "hermes-agent"

    obsidian_mcp_url: str = "http://127.0.0.1:27123/mcp/"
    obsidian_mcp_token: str = ""
    obsidian_daily_folder: str = (
        "📁 500 📒 Notes/📁 510 🧹 Maintenance/📁 511 🔍 Reviews/1. Daily"
    )
    obsidian_daily_format: str = "D-YYYY-MM-DD"
    obsidian_template_path: str = (
        "📁 300 💡 Resources/📁 330 㡯 Templates/Daily Template.md"
    )

    omnifocus_mcp_command: str = (
        "/Users/antonio/Dropbox/Code/Python/mcp/omnifocus-mcp/.venv/bin/omnifocus-mcp"
    )
    omnifocus_on_deck_perspective: str = "On Deck"
    omnifocus_tomorrow_perspective: str = "Tomorrow > 5 minutes"

    fantastical_mcp_command: str = (
        "/Applications/Fantastical.app/Contents/Helpers/"
        "FantasticalMCP.app/Contents/MacOS/FantasticalMCP"
    )

    kikodo_crm_mcp_command: str = (
        "/Users/antonio/micromamba/envs/kikodo-crm/bin/python"
    )
    kikodo_crm_mcp_args: str = (
        "/Users/antonio/Dropbox/Code/Python/Rocket/git/kikodo-crm/crm_mcp_server.py"
    )
    kikodo_crm_mcp_cwd: str = (
        "/Users/antonio/Dropbox/Code/Python/Rocket/git/kikodo-crm"
    )

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
