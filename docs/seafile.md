# Seafile Community Edition

The default configuration enables Seafile. To add it to an existing private configuration, append this entry to `apps`:

```json
{
  "name": "seafile",
  "kind": "seafile"
}
```

No Seafile settings are required. The preset bundles the official ARM64 Seafile 13 Community Edition image, MariaDB and Redis. Only Seafile's web port is published, on TCP 8080 by default; dependencies use a dedicated bridge network. This adapts the [official container deployment](https://manual.seafile.com/13.0/setup/setup_ce_by_docker/) to the stack's rootful Podman controller. SeaDoc, notification-server and AI extensions are disabled because their extra services are not deployed.

## First login

Seafile's official container creates its database and administrator automatically. It presents a login page, rather than a browser account-creation wizard. Open `http://<pi-address>:8080/`, sign in, change the generated password and configure Seafile through its normal UI.

When admin settings are omitted, the controller generates a random password and uses `admin@<appliance-hostname>.local` as the initial login. The private file `initial-admin.json` in that application's runtime directory records the email and original password. For an app named `seafile`, read it on the appliance as root:

```sh
cat /var/lib/home-stack/seafile/initial-admin.json
```

Use your configured SSH key or recovery console to access the appliance. If you will not have root access, supply initial admin settings before building. For the synthetic configuration directory's app named `files`, the runtime directory is `files` instead of `seafile`. Credentials are not included in normal status output, command arguments or build manifests. The file retains the original credentials after a password change; it does not reset the account. It also survives failed first-install retries and generation cleanup.

Without `server_url`, first installation selects the configured static IPv4 address or discovers the appliance's current IPv4 address through its default route. The selected web port is included in Seafile's generated URLs. Use a DHCP reservation: Seafile persists these absolute URLs, so an address change needs a deliberate configuration change. For DNS names or an existing reverse proxy, set `server_url` explicitly.

## Configuration

All existing configuration-file and configuration-directory inputs work. For a preconfigured installation:

```json
{
  "name": "seafile",
  "kind": "seafile",
  "auto_update": true,
  "settings": {
    "web_port": 8080,
    "server_url": "https://files.example.com",
    "admin_email": "owner@example.com",
    "admin_password": {"secret_file": "secrets/seafile-admin.secret"}
  }
}
```

The admin email and password must be supplied together; passwords require at least 12 characters. In a single JSON file, the password may also be a literal private string. Secret references follow the existing [configuration-directory rules](configuration-directory.md).

| Setting | Default and behaviour |
| --- | --- |
| `image` | `docker.io/seafileltd/seafile-mc:13.0-latest` |
| `auto_update` | `true`; uses the existing container timer |
| `settings.web_port` | `8080`; host TCP port mapped to container port 80 |
| `settings.server_url` | `null`; derive the initial LAN URL. Otherwise an HTTP/HTTPS URL with an optional port, without a path prefix, query, fragment or credentials |
| `settings.admin_email`, `settings.admin_password` | `null`; generate private initial credentials |
| `settings.database_image` | `docker.io/library/mariadb:10.11` |
| `settings.redis_image` | `docker.io/library/redis:8` |

`server_url` describes the address clients use. Setting it to HTTPS expects an existing proxy to terminate TLS and forward requests to the configured web port; this preset does not install a TLS proxy. Include a nonstandard public port in the URL when needed. The direct default listener uses HTTP on the LAN.

The appliance timezone also sets Seafile's `TIME_ZONE`. Mounts, networking, connection credentials, cache selection, initialization variables and feature switches are managed by the preset and cannot be replaced through `environment`. Generic partial shared-volume seeds are rejected: they can cause upstream initialization to skip creating required configuration or data. Use the preset settings for initialization and Seafile's settings afterward; restore an existing installation's files and database together through a deliberate maintenance operation.

Database passwords and the JWT key are generated on the appliance. Initial settings and credentials are applied only when creating a new data generation. Updates clone the active generation, including its configuration, and preserve later UI edits and password changes. Editing build inputs does not reconfigure an existing installation automatically.

## Updates and recovery

The default daily 03:00 container timer pulls all three moving tags before stopping any service. This follows Seafile 13.0 maintenance releases, MariaDB 10.11 maintenance releases and Redis 8 releases without routine manual image updates. A changed dependency alone also triggers a deployment. `auto_update: false` disables pulls while retaining recovery checks; an explicit version or digest can also pin an image.

The controller stops Seafile before its dependencies, copies the stopped files, configuration, database and private environment files, and starts MariaDB first. It waits for MariaDB's initialization/upgrade readiness check before starting Seafile. Redis ping, Seafile's unauthenticated web ping and the file-server protocol endpoint must pass consecutive health probes. Failed probation or an interrupted deployment restores the previous complete image set and its untouched data. Partial pull failures leave the active group running. Failures use the existing cooldown, status and optional notification system.

Tags stay within supported major series. Future major upgrades can require new services, settings or explicit database migrations, as described in the [upstream upgrade guide](https://manual.seafile.com/13.0/upgrade/upgrade_docker/). Those deployment changes require a controller/configuration update; arbitrary future major versions cannot be promised unattended compatibility.

## Storage and acceptance

Each generation contains the entire Seafile shared directory, MariaDB data, Redis data directory and private environment files. Seafile's internal configuration and application log files therefore persist on SSD even though container stdout/stderr collection is disabled. Redis is a disposable cache with persistence disabled. Btrfs reflinks reduce the initial cost of snapshot copies, but uploaded files, database changes and retained generations still require free space.

Allow room for libraries, all bundled images and rollback data when selecting `image_size_mib`; the default image remains 32 GiB. Allow additional RAM for Seafile and MariaDB alongside the other services. Back up Seafile's files and database independently of this SSD. A later rollback can discard changes made since the retained generation.

Local tests simulate container execution and failure recovery. A live Seafile deployment has not been validated on this Pi/MicroOS image. Before relying on it, verify first login, library creation, upload/download and desktop sync, URL generation behind your proxy if used, reboot persistence, SELinux permissions and a real update/rollback with both files and database preserved.
