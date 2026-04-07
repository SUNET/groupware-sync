"""Database URL resolution.

The auth helper picks its database URL from the environment, falling back to a
local SQLite file under XDG data home. The default path is created lazily so
that running with a non-default URL (e.g. mysql) does not touch the local
filesystem.
"""
import os
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "groupware-sync" / "auth.db"
ENV_VAR = "GROUPWARE_SYNC_DATABASE_URL"


def get_database_url() -> str:
    url = os.environ.get(ENV_VAR)
    if url:
        return url
    return f"sqlite:///{DEFAULT_DB_PATH}"


def ensure_data_dir() -> None:
    """Create the parent directory of the default sqlite file if needed.

    No-op when GROUPWARE_SYNC_DATABASE_URL is set, because the user is then
    responsible for the connection target.
    """
    if os.environ.get(ENV_VAR):
        return
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
