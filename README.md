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
after upgrading from a version older than `0a6dc40`; steady-state sync
won't rewrite them on its own.

## Security model

Token storage is intentionally simple, and operators should treat the
backing databases as the trust boundary for live API access:

- **OAuth access tokens** live in plain columns in the database pointed at
  by `SYNC_STALWART_AUTH_DATABASE_URL` / `SYNC_M365_AUTH_DATABASE_URL`
  (`oc_ioidc_userconfig.access_token` in Nextcloud's
  [`integration_oidc`](https://github.com/julien-nc/integration_oidc) schema,
  or the auth helper's own SQLite DB at `~/.local/share/groupware-sync/auth.db`).
- **OAuth client secrets** for providers configured via the auth helper live
  in the same DB (`oc_ioidc_providers.client_secret`).
- **CardDAV/CalDAV passwords** are read from `SYNC_SIDE_{A,B}_DAV_PASSWORD`
  env vars at runtime; no on-disk storage by this tool.

Anyone who can read those databases can impersonate the synced user against
the configured backends until the access token expires (refresh-token
rotation is handled by Nextcloud's `integration_oidc`, not by this tool).
Practical guidance for operators:

- Restrict filesystem permissions on the SQLite DB to the sync user
  (`chmod 600`) and keep it on the same host as the cron job.
- For MySQL/Postgres-backed Nextcloud DBs, use a dedicated read-only DB
  user scoped to the `oc_ioidc_*` tables.
- Rotate provider client secrets and revoke access tokens out of the
  Nextcloud admin UI if the DB is ever exposed.
- Do not commit `.env` files containing `SYNC_*_AUTH_*` or
  `SYNC_SIDE_*_DAV_PASSWORD` values.

This mirrors Nextcloud's own model — the tokens have to be readable by the
sync process, and we deliberately don't add a homegrown encryption-at-rest
layer that would only have its key sitting next to the data anyway.

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
