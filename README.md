# groupware-sync-auth

OAuth helper for local development of groupware-sync. Performs device code flow
against Microsoft 365 and Stalwart, stores access tokens in a SQLAlchemy database
that matches the production `nextcloud.oc_ioidc_userconfig` schema, and refreshes
them on a systemd user timer.

This is a dev tool. Production uses the Nextcloud
[integration_oidc](https://github.com/SUNET/nextcloud-integration_oidc.git) app.

## Install

    python -m venv .venv
    source .venv/bin/activate
    pip install -e .

## Quick start

    groupware-sync-auth provider add --name m365 \
        --client-id <uuid> \
        --device-authorization-endpoint https://login.microsoftonline.com/<tenant>/oauth2/v2.0/devicecode \
        --token-endpoint                https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token \
        --user-endpoint                 https://graph.microsoft.com/oidc/userinfo \
        --scope "offline_access Mail.ReadWrite Calendars.ReadWrite Contacts.ReadWrite User.Read"

    groupware-sync-auth login --provider m365
    groupware-sync-auth list
    groupware-sync-auth install-systemd
