# groupware-sync

Two-way contacts sync between Stalwart (JMAP), Microsoft 365 (Graph API), and
CardDAV servers (Radicale, etc.) — built on a generic tree-based sync framework
with Merkle-style subtree hashing for change detection and field-level merge.

## What it does

- Syncs contacts bidirectionally between any pair of supported backends
- Detects changes efficiently using Merkle-style subtree hashes (skip unchanged subtrees)
- Merges field-level edits from both sides (last-write-wins for conflicts)
- Identity-matches contacts by email address on first sync
- Supports 18 contact fields: name, email, phone, address, organization,
  department, job title, birthday, nickname, website, photo, and more

## Supported protocols

| Protocol | Backend | Adapter |
|---|---|---|
| JMAP (RFC 8620) + JSContact (RFC 9553) | Stalwart Mail Server | `JmapContactAdapter` |
| Microsoft Graph REST API v1.0 | Microsoft 365 / Exchange Online | `GraphContactAdapter` |
| CardDAV (RFC 6352) + WebDAV Sync (RFC 6578) | Radicale, Nextcloud, etc. | `CardDavContactAdapter` |

## Architecture

Three packages in one repo:

- **`groupware_sync`** — generic tree-based sync framework (protocol-agnostic)
- **`groupware_sync_contacts`** — contact-specific adapters and field mapping
- **`groupware_sync_auth`** — OAuth 2.0 token management (device code flow)

The sync framework represents each endpoint's data as a tree of nodes with
fingerprints. Subtree hashes (Merkle-style, used for pruning — not
cryptographic commitments or proof paths) allow the engine to skip unchanged
branches. A recursive three-way comparison (tree A, tree B, stored state)
produces a minimal set of sync operations. Field-level merge uses configurable
strategies per field (SCALAR, SET, or IMMUTABLE).

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

### 1. Set up OAuth tokens

For Stalwart (device code flow via the built-in auth helper):

```bash
groupware-sync-auth provider add --name stalwart \
    --client-id <client_id> \
    --device-authorization-endpoint https://mail.example.org/auth/device \
    --token-endpoint https://mail.example.org/auth/token \
    --user-endpoint https://mail.example.org/auth/userinfo \
    --scope "openid offline_access"

groupware-sync-auth login --provider stalwart
```

For Microsoft 365, tokens come from a Nextcloud `integration_oidc` database or
the auth helper (if Entra Conditional Access allows device code flow).

### 2. Configure and run contacts sync

All configuration via environment variables:

```bash
export SYNC_STALWART_JMAP_URL=https://mail.example.org
export SYNC_STALWART_AUTH_DATABASE_URL=sqlite:////path/to/auth.db
export SYNC_STALWART_AUTH_UID=username
export SYNC_STALWART_AUTH_PROVIDER_NAME=stalwart
export SYNC_STALWART_ADDRESSBOOK="My Contacts"

export SYNC_M365_AUTH_DATABASE_URL=mysql+pymysql://user:pass@host/nextcloud
export SYNC_M365_AUTH_UID=user@example.com
export SYNC_M365_AUTH_PROVIDER_NAME=m365
export SYNC_M365_ADDRESSBOOK=Contacts

export SYNC_STATE_DATABASE_URL=sqlite:////path/to/sync-state.db

# Dry run (show what would happen):
groupware-sync-contacts sync --dry-run --verbose

# Real sync:
groupware-sync-contacts sync --verbose
```

### 3. Selecting a backend per side

By default side A is Stalwart (JMAP) and side B is Microsoft 365 (Graph). To
sync against a CardDAV or CalDAV server instead, set the per-side backend
selector and provide DAV credentials for that side:

```bash
# Replace side B (the M365 side) with a CardDAV server:
export SYNC_SIDE_B_BACKEND=carddav   # or pass --side-b-backend carddav
export SYNC_SIDE_B_DAV_URL=https://radicale.example.org
export SYNC_SIDE_B_DAV_USERNAME=alice
export SYNC_SIDE_B_DAV_PASSWORD=...

groupware-sync-contacts sync --verbose
```

| Side | Backend selector value | Required env vars |
|---|---|---|
| Stalwart JMAP | `jmap` *(default for side A)* | `SYNC_STALWART_*`, `SYNC_STALWART_JMAP_URL` |
| Microsoft Graph | `graph` *(default for side B)* | `SYNC_M365_*` |
| CardDAV / CalDAV | `carddav` / `caldav` | `SYNC_SIDE_{A,B}_DAV_URL`, `SYNC_SIDE_{A,B}_DAV_USERNAME`, `SYNC_SIDE_{A,B}_DAV_PASSWORD` |

The contacts CLI accepts `jmap`, `graph`, and `carddav`. The calendar CLI
accepts `jmap`, `graph`, and `caldav`. CLI flags `--side-a-backend` and
`--side-b-backend` override the env vars for one-off invocations.

### 4. Calendar sync

`groupware-sync-calendar` follows the same conventions as the contacts CLI
and shares all of the auth env vars above. Pick which calendar to sync per
side with `SYNC_STALWART_CALENDAR` / `SYNC_M365_CALENDAR` (omit either to
sync every calendar on that side):

```bash
export SYNC_STALWART_CALENDAR="Calendar"
export SYNC_M365_CALENDAR="Calendar"

# Dry run:
groupware-sync-calendar sync --dry-run --verbose

# Real sync:
groupware-sync-calendar sync --verbose
```

There is also a one-shot `groupware-sync-calendar repair-jmap-alerts`
command for cleaning up older JMAP events whose VALARMs landed without a
proper `@type=Alert` (these crash Stalwart's calendar UI). Run it once
if you have legacy events created by older builds that wrote alerts
without `@type=Alert`; steady-state sync won't rewrite them on its own.

## Security model

Where credentials live and what an attacker with read access to each store
can do. The trust boundary depends on which OAuth path you use.

**OAuth access tokens** are read from the database pointed at by
`SYNC_STALWART_AUTH_DATABASE_URL` / `SYNC_M365_AUTH_DATABASE_URL`. Two
paths are supported:

- **Nextcloud `integration_oidc`** — access tokens are stored in plain
  columns of the
  [`integration_oidc`](https://github.com/julien-nc/integration_oidc)
  schema (`oc_ioidc_userconfig.access_token`). Refresh-token storage and
  rotation are owned by Nextcloud, not this tool. Anyone who can read
  those rows has live API access until the access token expires.
- **Auth helper (`groupware-sync-auth`)** — access tokens (and
  `oc_ioidc_providers.client_secret` for providers configured via the
  helper) live in the helper's SQLite DB at
  `~/.local/share/groupware-sync/auth.db`. Refresh tokens live in the OS
  keyring; `groupware-sync-auth tick` / `refresh` uses them to rotate
  access tokens. In this mode the keyring is part of the trust boundary
  too — the SQLite DB alone is not the only secret-bearing component.

**CardDAV/CalDAV passwords** are read from `SYNC_SIDE_{A,B}_DAV_PASSWORD`
env vars at runtime; this tool does not persist them.

Practical guidance for operators:

- Restrict filesystem permissions on `auth.db` to the sync user
  (`chmod 600`) and keep it on the same host as the cron job.
- For MySQL/Postgres-backed Nextcloud DBs, use a dedicated read-only DB
  user scoped to the `oc_ioidc_*` tables.
- When running the auth helper, protect the OS keyring/session it writes
  to — exposed keyring access is equivalent to exposed refresh tokens.
- Rotate client secrets and revoke access tokens via the appropriate
  admin UI (Nextcloud or the OAuth provider) if any of these stores are
  exposed.
- Do not commit `.env` files containing `SYNC_*_AUTH_*` or
  `SYNC_SIDE_*_DAV_PASSWORD` values.

This mirrors the underlying systems' models — the sync process has to be
able to read whatever token material its OAuth path relies on, and we
deliberately don't add a homegrown encryption-at-rest layer whose key
would sit next to the data anyway.

## Testing

Unit tests (no external dependencies):

```bash
pytest tests/test_*.py -v
```

End-to-end tests against local Docker services:

```bash
docker compose up -d
pytest tests/e2e/ -m e2e -v
docker compose down
```

## License

[GPL-3.0-or-later](LICENSE)
