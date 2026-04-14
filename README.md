# groupware-sync

Two-way contacts sync between Stalwart (JMAP), Microsoft 365 (Graph API), and
CardDAV servers (Radicale, etc.) — built on a generic tree-based sync framework
with Merkle-tree change detection and field-level merge.

## What it does

- Syncs contacts bidirectionally between any pair of supported backends
- Detects changes efficiently using Merkle hashes (skip unchanged subtrees)
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
fingerprints. A recursive three-way Merkle comparison (tree A, tree B, stored
state) produces a minimal set of sync operations. Field-level merge uses
configurable strategies per field (SCALAR, SET, or IMMUTABLE).

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
groupware-sync-contacts sync-v2 --dry-run --verbose

# Real sync:
groupware-sync-contacts sync-v2 --verbose
```

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
