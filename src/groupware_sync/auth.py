"""Read access tokens from an auth database.

Same contract as groupware_sync_contacts.auth — bare SELECT against
the oc_ioidc_* schema. No dependency on groupware_sync_auth.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text


def get_access_token(database_url: str, uid: str, provider_name: str) -> str:
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
