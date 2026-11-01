# Synthetic configuration directory

Every value here is an invented example or a software default. The secret files are deliberately fake placeholders; replace them in your own private directory. This directory contains no real devices, network identities, user credentials or location.

From the public repository: `python3 build.py --config-dir examples/configuration --render-only build/example`.

Immich starts with its usual onboarding and automatic stable-release updates. Its `immich/immich-config.json` placeholder is optional and inactive. To enable file-managed settings, add `"seed": "immich"` to the photos app and set `settings.config_file` to `"immich-config.json"`. This makes Immich settings read-only in the web UI. See [Immich configuration](../../docs/immich.md).

OpenCloud initializes automatically at `https://<pi-address>:9200/` with a locally generated certificate. Sign in as `admin`. This example omits the initial password, so the appliance generates private credentials in the `files` application's `initial-admin.json`; alternatively supply `settings.admin_password` through a secret reference before building. Configuration and file data are preserved together during automatic updates and rollback. See [OpenCloud configuration and first login](../../docs/opencloud.md).
