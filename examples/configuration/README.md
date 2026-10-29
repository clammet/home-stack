# Synthetic configuration directory

Every value here is an invented example or a software default. The secret files are deliberately fake placeholders; replace them in your own private directory. This directory contains no real devices, network identities, user credentials or location.

From the public repository: `python3 build.py --config-dir examples/configuration --render-only build/example`.

Immich starts with its usual onboarding and automatic stable-release updates. Its `immich/immich-config.json` placeholder is optional and inactive. To enable file-managed settings, add `"seed": "immich"` to the photos app and set `settings.config_file` to `"immich-config.json"`. This makes Immich settings read-only in the web UI. See [Immich configuration](../../docs/immich.md).

Seafile initializes automatically on port 8080 and opens its normal login page. This example omits initial credentials, so the appliance generates a private password; retrieve it from the `files` application's `initial-admin.json`, or supply your own email/password settings before building. Seafile, MariaDB and Redis receive automatic maintenance updates together. See [Seafile configuration and first login](../../docs/seafile.md).
