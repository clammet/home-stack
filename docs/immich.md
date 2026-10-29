# Immich

The default configuration enables Immich with normal first-run onboarding and automatic stable-release updates. Add this entry to an existing private configuration's `apps` array if needed:

```json
{
  "name": "immich",
  "kind": "immich",
  "image": "ghcr.io/immich-app/immich-server:release",
  "auto_update": true,
  "settings": {"web_port": 2283, "config_file": null}
}
```

Build and install the appliance as described in the main README, then open `http://<pi-address>:2283/` to create the administrator and finish onboarding. No administrator is created automatically, and no old accounts, photos or database are imported. With `config_file` left as `null`, Immich's web UI manages its settings.

## Deployment

Immich is a group of four containers using the existing rootful Podman controller:

- Immich server, published on the selected TCP web port.
- Immich machine learning, using the matching server image tag by default.
- Immich's PostgreSQL image with VectorChord support.
- Valkey, the Redis-compatible queue/cache used by the upstream deployment.

The database, queue and worker use a dedicated bridge network with DNS aliases and have no published host ports. First boot explicitly installs Netavark and Aardvark DNS. Home Stack generates a random database password on the appliance and passes credentials through private environment files. The image builder bundles and validates all four images for `linux/arm64`; the output manifest includes each image reference and archive checksum.

This adapts the [v3.3.5 release deployment](https://github.com/immich-app/immich/blob/v3.3.5/docker/docker-compose.yml). Upstream's recommended installation uses [Docker Compose](https://docs.immich.app/install/docker-compose/); this Podman adaptation still needs acceptance testing on MicroOS/Pi hardware.

## Optional preconfiguration

Home Stack supports Immich's native [configuration file](https://docs.immich.app/install/config-file/). A placeholder is supplied at `examples/configuration/immich/immich-config.json`. To use it, copy the `immich` directory into your private configuration directory and change the app entry to:

```json
{
  "name": "immich",
  "kind": "immich",
  "seed": "immich",
  "settings": {"config_file": "immich-config.json"}
}
```

`config_file` is relative to that app's seed directory and must contain a JSON object. Home Stack validates confinement and JSON syntax; Immich validates the supported settings for the installed release. The controller mounts the seeded configuration read-only and sets `IMMICH_CONFIG_FILE` automatically. Other seed files, including explicitly selected private overlays, follow the existing [configuration-directory rules](configuration-directory.md).

The placeholder includes database-backup retention, machine learning, password login, disabled OAuth and a disabled storage template. Replace or extend it with the settings supported by your Immich release, such as SMTP or OAuth. For private settings, use `seed.private_files` to map an explicitly named JSON file from `secrets/` to `immich-config.json`; do not put credentials in the public example. Secret references inside a seed file's contents are not expanded.

**Selecting a config file makes file configuration authoritative and disables editing settings in Immich's web UI**, even when the file includes only some settings. Account creation and normal onboarding remain available. Leave `config_file` unset for the default UI-managed experience. Seeds apply only to the initial generation; updates copy existing data and never reapply them. Updating an existing installation's seeded settings requires an explicit maintenance change.

Additional supported [environment variables](https://docs.immich.app/install/environment-variables/), such as `IMMICH_LOG_LEVEL` and `MACHINE_LEARNING_MODEL_TTL`, can be supplied through `apps[].environment`. Worker tuning variables are also passed to the ML container. Home Stack reserves connection, port, mount and worker-selection variables needed by the preset; use `settings.web_port` to change the published port. `TZ` follows the appliance timezone.

## Updates and recovery

`release` follows Immich's latest stable release using Home Stack's container update timer (daily at 03:00 by default). It does not select prerelease images. Server and machine-learning tags are paired automatically. To pin a version, set the server image to a version tag; a custom registry or digest-pinned server requires an explicit matching `settings.machine_learning_image`. Set `auto_update` to `false` to disable pulls while retaining runtime recovery.

PostgreSQL and Valkey default to the dependency images pinned in the v3.3.5 release compose file. They are deliberately not assigned `latest` tags: unattended database major upgrades require a separate migration. Advanced deployments can override `settings.database_image` and `settings.redis_image`, with responsibility for compatibility and migration.

Before any deployment, the controller resolves every image, stops the server and worker before the database and queue, and clones the entire stopped data generation. The new group starts database/queue first and server last. PostgreSQL readiness, Valkey ping, machine-learning HTTP health and the server's public ping must pass probation. No account is needed for these checks. Failure or an interrupted deployment restores the prior image set and its own untouched data. A dependency-only image change also triggers a group deployment; a partial pull failure leaves the active group running.

[Immich upgrades](https://docs.immich.app/install/upgrading/) can require deployment changes, particularly across major versions; automatic image pulls cannot implement every future upstream migration. Availability checks also cannot prove photo processing or mobile-client compatibility. Home Stack records failed updates and uses its existing cooldown/notification system. Changes to this controller or dependency defaults require a rebuild or deliberate maintenance deployment.

## Storage and hardware

Each retained generation holds media, PostgreSQL data, persistent Valkey queue data, the model cache, configuration and database credentials. Keeping all persistent state together allows an update rollback to restore a consistent application state. Reflinks avoid initially duplicating unchanged media on Btrfs; changed files and retained generations still need free space. Model weights download on demand, so bundling the worker image does not make all machine-learning features available offline.

Size `image_size_mib` for your photo library, thumbnails, transcoded video, database, bundled images and rollback space before building. The default 32 GiB image is suitable for experimentation; a larger SSD's unused capacity is not allocated automatically. Keep independent backups of both media and database. A later rollback can discard uploads or other changes made since its retained snapshot.

Upstream currently specifies [at least 6 GB RAM and recommends 8 GB](https://docs.immich.app/install/requirements/). Allow additional memory for Home Assistant and Pi-hole on the same Pi. Complete first-run onboarding, upload and retrieve a photo, verify processing/search, reboot persistence, SELinux labels and a real update/rollback on the target before relying on the appliance.
