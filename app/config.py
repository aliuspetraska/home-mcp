import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# MCP clients start the server with an arbitrary working directory, so resolve
# the .env relative to the project root rather than the cwd.
ENV_FILE = Path(
    os.environ.get("HOME_MCP_ENV_FILE", Path(__file__).resolve().parent.parent / ".env")
)
load_dotenv(ENV_FILE, override=False)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, loaded from environment / .env file."""

    # myUplink (NIBE) — https://dev.myuplink.com/ → "Applications" → create app
    myuplink_client_id: str
    myuplink_client_secret: str

    # Optional overrides
    myuplink_base_url: str
    myuplink_token_url: str

    meteo_base_url: str

    http_timeout_seconds: float

    # Defaults — change here rather than via env vars
    myuplink_language: str = "en-GB"
    myuplink_read_only: bool = False

    @property
    def myuplink_configured(self) -> bool:
        return bool(self.myuplink_client_id and self.myuplink_client_secret)

    @property
    def myuplink_scopes(self) -> str:
        return "READSYSTEM" if self.myuplink_read_only else "READSYSTEM WRITESYSTEM"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        myuplink_client_id=os.getenv("MYUPLINK_CLIENT_ID", ""),
        myuplink_client_secret=os.getenv("MYUPLINK_CLIENT_SECRET", ""),
        myuplink_base_url=os.getenv("MYUPLINK_BASE_URL", "https://api.myuplink.com"),
        myuplink_token_url=os.getenv(
            "MYUPLINK_TOKEN_URL", "https://api.myuplink.com/oauth/token"
        ),
        meteo_base_url=os.getenv("METEO_BASE_URL", "https://api.meteo.lt/v1"),
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
    )
