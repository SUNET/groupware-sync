"""Read access tokens from an auth database.

This module has NO dependency on groupware_sync_auth. It performs a bare
SELECT against the oc_ioidc_* schema — the same contract the auth helper
writes to and Nextcloud's integration_oidc maintains in production.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text


def get_access_token(database_url: str, uid: str, provider_name: str) -> str:
    """Fetch the current access token for a (uid, provider_name) pair.

    Raises ValueError if no matching row is found.
    """
    engine = create_engine(database_url, echo=False)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT u.access_token"
                " FROM oc_ioidc_userconfig u"
                " JOIN oc_ioidc_providers p ON p.id = u.provider_id"
                " WHERE u.uid = :uid AND p.name = :provider_name"
            ),
            {"uid": uid, "provider_name": provider_name},
        ).first()
    engine.dispose()
    if row is None:
        raise ValueError(
            f"no access token for uid={uid!r} provider={provider_name!r}"
        )
    return row[0]
