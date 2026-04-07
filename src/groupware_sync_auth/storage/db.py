"""SQLAlchemy models matching the production nextcloud.oc_ioidc_* schema.

`oc_ioidc_userconfig` deliberately exposes only the columns the future sync
tools will SELECT (id, email, provider_id, access_token, uid). Refresh tokens
and expiry tracking live in the OS keyring, not here. See storage/secrets.py.

`oc_ioidc_providers` is the helper's private OAuth client registry, not part of
the contract with sync tools. It adds one column production does not have:
device_authorization_endpoint (because the helper uses device code flow and
integration_oidc uses authorization code flow).
"""
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from groupware_sync_auth.config import ensure_data_dir, get_database_url


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "oc_ioidc_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    device_authorization_endpoint: Mapped[str] = mapped_column(String(255))
    token_endpoint: Mapped[str] = mapped_column(String(255))
    user_endpoint: Mapped[str] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(Text)


class UserConfig(Base):
    __tablename__ = "oc_ioidc_userconfig"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("oc_ioidc_providers.id"))
    access_token: Mapped[str] = mapped_column(Text)
    uid: Mapped[str] = mapped_column(String(255))


_engine = None
_SessionLocal: Optional[sessionmaker] = None


def _init() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        return
    ensure_data_dir()
    _engine = create_engine(get_database_url(), echo=False, future=True)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def get_session() -> Session:
    _init()
    assert _SessionLocal is not None
    return _SessionLocal()
