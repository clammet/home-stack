# UniFi Network controller

The optional `unifi` preset runs the self-hosted UniFi Network application using the ARM64 [jacobalberty/unifi image](https://github.com/jacobalberty/unifi-docker). That image includes MongoDB, so its configuration and database share one managed data generation. This manages UniFi network devices; it does not install UniFi OS, Protect, Talk or Access.

Add this entry to the `apps` array in your private configuration:

```json
{
  "name": "unifi",
  "kind": "unifi",
  "settings": {
    "backup": "secrets/network.unf",
    "inform_host": "192.0.2.10"
  }
}
```

Replace the example inform address with the appliance's reserved LAN address or a hostname your devices can resolve. Omit `backup` for normal browser onboarding. The public `config.example.json` includes a disabled UniFi entry; enable it to deploy it. UniFi's TCP 8080 device listener coexists with OpenCloud's default port 9200; the loader rejects conflicting custom ports.

Open `https://<pi-address>:8443/`. UniFi generates a self-signed certificate. A restored installation uses the accounts from the backup. Without a backup, complete UniFi's setup wizard.

## Backup input

Export a **UniFi Network application backup (`.unf`)** from the existing controller. [Ubiquiti's migration instructions](https://help.ui.com/hc/en-us/articles/360008976393-Backups-and-Migration-in-UniFi) describe exporting and restoring this application backup. Console-wide UniFi OS `.unifi` backups and site exports are not accepted by this preset.

Store the file in your private configuration directory, preferably under `secrets/`, and reference it with `settings.backup`. Paths are relative to the JSON file for `--config`, or to the configuration directory for `--config-dir`. Absolute paths, traversal and symlinks are rejected. Backups must be nonempty regular files of at most 128 MiB. Unlike secret text, backup bytes are not decoded as UTF-8 or stripped of line endings. The builder treats the format as opaque; UniFi checks its contents and version compatibility.

The resolved configuration embeds the backup for transfer to the Linux builder on macOS and inclusion in the private image. No source filename or host filesystem path is needed on the appliance. Rendered payloads, VM input archives and images therefore contain network credentials and must stay private. The public checkout ignores `.unf` and `.unifi` files; configure equivalent exclusions in your private repository if its backups should not be committed.

Use a destination application version compatible with the source backup, normally the same version or newer. Override `image` with a compatible upstream version tag or digest when commissioning. A newer backup cannot be made compatible by renaming it. Large restores may need a larger `updates.health_timeout_seconds` value (maximum 3600).

## First initialization

Restoration uses the application's initial setup API, following the [bootstrap implementation's upload and restore sequence](https://github.com/mhosker/terraform-unifi-bootstrap). It does not unpack the backup into database files or fabricate application configuration:

1. Create an empty generation and a private pending-restore record.
2. Start UniFi and wait for its local readiness endpoint.
3. Check that the embedded database contains no administrator, refusing to overwrite a configured controller.
4. Upload the backup to the local setup endpoint and submit its returned backup identifier for restoration.
5. Wait for the image's container restart, a ready controller and a restored administrator before recording completion. Normal consecutive health probation must then pass before accepting the generation.

The API connection is fixed to loopback, bypasses proxies and refuses redirects. Certificate verification is disabled only for that local connection because the application generates a self-signed certificate. Backup bytes and API responses are not logged or included in command arguments or normal status output.

The restore record follows the data generation through update and rollback copies. Completed restores are never repeated over subsequent UI changes. Changing `settings.backup` does not import a new backup into an existing installation. Without an initial backup, later adding one also does not overwrite the running controller.

A rejected upload, incompatible backup, missing restored administrator or restore timeout prevents acceptance. The existing deployment journal stops and removes an unsuccessful first installation, discards its partial database, records a fault and applies the normal retry cooldown. An interrupted deployment is discarded during recovery before a later attempt creates another fresh generation. A submitted restore is never replayed against the same potentially modified database.

These setup endpoints are application internals, not a stable public provisioning API. The preset deliberately follows the selected image's embedded database and restart behaviour. An unrelated UniFi image, including LinuxServer's image that requires external MongoDB, is not a drop-in replacement.

## Settings and ports

| Setting | Default and behaviour |
| --- | --- |
| `image` | `docker.io/jacobalberty/unifi:latest`; may be pinned to a compatible upstream version/digest |
| `auto_update` | `true`; uses the existing daily container update timer |
| `settings.backup` | `null`; otherwise a relative `.unf` input, applied only to a new installation |
| `settings.web_port` | `8443`; host TCP port mapped to the application's HTTPS port 8443 |
| `settings.inform_host` | `null`; otherwise the advertised LAN IPv4 address or hostname, passed as `SYSTEM_IP` |
| `settings.guest_portal` | `false`; true additionally publishes TCP 8880 and 8843 |

TCP 8080 (device inform), UDP 3478 (STUN) and UDP 10001 (discovery) are published along with the management web port. MongoDB is internal and never published. Speed-test and syslog listeners are not exposed by this preset. Only the listed host ports are opened in the appliance firewall. Bridges and routed VLANs may require explicit device inform configuration; broadcasting discovery through a container bridge is not guaranteed.

Use a DHCP reservation or a static address. Existing devices must be directed to the new controller using Ubiquiti's migration process or the existing controller's inform override. Importing a backup does not change device-side inform addresses by itself. The old controller should not remain active against the same devices during migration. After restoration, confirm the advertised inform address and guest portal settings in UniFi.

The preset runs the application as the image's `unifi` user and keeps runtime PID files in a temporary mount. Managed database, startup, path and port environment values cannot be overridden. Additional tuning such as `JVM_MAX_HEAP_SIZE` can be supplied through `environment`. Application logs and UniFi's own automatic backups remain in its persistent data tree; container stdout/stderr collection is disabled.

## Updates and validation

The normal update process pulls before stopping UniFi, copies its stopped configuration and database together, then starts the replacement against the copy. Health probes require the application to report ready. Failed updates restore the previous image and its untouched data. A local rollback can discard changes made since the retained generation; keep independent UniFi backups outside the SSD.

Local tests cover binary transfer, input validation, ARM64 bundling, ports, API response parsing, restoration checks, preservation of completion records and failed-install cleanup. **A real backup restoration and Pi/MicroOS deployment have not been validated.** Before relying on automatic restoration, commission with a compatible pinned image and a copy of your backup, then verify restored sites, administrator access, device reconnection, reboot persistence and a real update/rollback. The moving tag can change the internal setup API or database behaviour, which may require a preset update.
