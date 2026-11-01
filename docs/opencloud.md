# OpenCloud

The default configuration enables OpenCloud. Add it to an existing private configuration with:

```json
{
  "name": "opencloud",
  "kind": "opencloud"
}
```

The preset bundles the official production ARM64 image, `docker.io/opencloudeu/opencloud:latest`. One container provides file sync, sharing, the web client and embedded identity services; no separate SQL database or Redis container is needed. It adapts the [upstream single-container installation](https://docs.opencloud.eu/docs/admin/getting-started/container/docker/) to the rootful Podman controller. Online office, external identity providers and additional services are outside this preset.

## First login

Open `https://<pi-address>:9200/` and accept the locally generated certificate on your trusted LAN. Sign in as `admin`. Supply `settings.admin_password` before building, or retrieve the generated username/password from `initial-admin.json` in the application's runtime directory on the appliance. For the default name that directory is `opencloud`; the synthetic example calls it `files`.

Use your configured SSH key or recovery console for root access to the appliance's home-stack state directory. If you will not have root access, provide an initial password before building. Initial credentials are stored privately with mode `0600`, stay stable across first-install retries, and are excluded from status output and command arguments. Change the password through OpenCloud after signing in. The credential file retains the original password and does not reset an existing account.

Without `server_url`, initialization selects the static IPv4 address or discovers the appliance's current IPv4 address using its default route. The generated HTTPS URL includes the selected web port and is retained with the data generation. Use a DHCP reservation. OpenCloud's browser, desktop clients and the container itself must all be able to reach that same canonical URL, including through any reverse proxy. A local health probe does not establish that external routing works.

For a configuration directory, an explicit password can use a relative secret reference:

```json
{
  "name": "opencloud",
  "kind": "opencloud",
  "settings": {
    "web_port": 9200,
    "server_url": "https://files.example.test:9200",
    "admin_password": {"secret_file": "secrets/opencloud-admin.secret"}
  }
}
```

The initial username is always `admin`. Passwords must contain at least 12 characters. With a standalone JSON file, a literal password is also accepted.

## Settings and HTTPS reverse proxies

| Setting | Default / meaning |
| --- | --- |
| `image` | `docker.io/opencloudeu/opencloud:latest`; override with an upstream-compatible version or digest |
| `auto_update` | `true`; follow the production image through the daily timer |
| `settings.web_port` | `9200`, published to the container's fixed port 9200 |
| `settings.server_url` | `null`; derive the LAN HTTPS URL once on first initialization |
| `settings.admin_password` | `null`; generate a private initial password |
| `settings.proxy_tls` | `true`; OpenCloud serves HTTPS with its generated certificate |
| `settings.insecure` | `true`; OpenCloud accepts self-signed certificates for its internal connections |

`server_url` must be an HTTPS origin with a hostname or IPv4 address, an optional port, and no subpath, query, fragment or credentials. It is the address clients use, which can differ from the appliance's published port when a reverse proxy is present.

For an existing reverse proxy with a trusted public certificate:

```json
{
  "name": "opencloud",
  "kind": "opencloud",
  "settings": {
    "server_url": "https://files.example.test",
    "proxy_tls": false,
    "insecure": false
  }
}
```

Forward the proxy to the appliance using HTTP on port 9200 and preserve the public host and HTTPS forwarding headers. Protect that backend port from untrusted networks. The appliance still opens the configured port in its firewall; the preset does not deploy or manage your reverse proxy. Disabling `proxy_tls` requires an explicit HTTPS `server_url`. Direct generated certificates require `insecure: true`; certificate verification can be enabled with the trusted reverse proxy configuration above. See [upstream proxy deployment](https://github.com/opencloud-eu/opencloud-compose).

Storage, identity, networking and initialization environment values are managed. Log tuning through `OC_LOG_LEVEL`, `OC_LOG_COLOR`, `OC_LOG_PRETTY`, and language through `OC_DEFAULT_LANGUAGE` are allowed. Partial configuration/data seeds are rejected so initialization cannot skip required secrets or create an incomplete identity store. The image runs as UID/GID 1000 with both persistent mounts owned accordingly; private credential/environment files remain root-only.

## Updates and recovery

Initialization runs `opencloud init` against the bundled image once. A failed initialization is surfaced; its error is not ignored. Subsequent starts run `opencloud server` against existing configuration without regenerating secrets or accounts.

The daily 03:00 timer pulls the configured image before stopping the service. It copies the stopped configuration directory, complete data directory and private environment as one generation, then starts the candidate image. Consecutive health probes require a running container, an installed/non-maintenance response from `status.php`, and valid OpenID discovery with the persisted issuer URL. These unauthenticated checks do not depend on the initial password remaining unchanged. Self-signed certificate acceptance in the controller's probe is confined to the fixed loopback endpoint; redirects are rejected.

Failed probation or an interrupted deployment restores the previous image and untouched data. Existing cooldown, status and optional fault notifications apply. An image pull failure leaves the active service running. `auto_update: false` retains recovery checks while disabling automatic pulls.

The moving production tag can introduce migrations or future changes requiring controller/configuration work. Pin a version or digest to control adoption. Local rollback does not prove compatibility with every future release or replace independent backups. A later rollback discards changes made since its retained generation.

## Storage and acceptance

Both configuration and data are stored on SSD inside the same managed generation, including embedded identity data, storage metadata and files. Allow room for uploaded data, images and rollback generations when selecting `image_size_mib`; the default image remains 32 GiB. Back up both persistent directories together from a stopped service, along with private environment/credentials. Copying uploaded files alone is not a complete restore.

Local tests simulate container execution. The upstream production image manifest was checked for Linux/ARM64 support, but live first login, upload/download, desktop sync, reboot persistence, Podman/SELinux permissions and a real image update/rollback still need acceptance on the Pi. Verify the canonical URL from both clients and inside the container, including reverse proxy routing if used.

Seafile support has been removed. Existing private configurations must replace its app entry with `kind: "opencloud"` and OpenCloud settings before building. Use a new application name for an existing installation: Seafile generations and databases cannot be reused as OpenCloud data. Export and transfer any files deliberately; this change neither migrates nor deletes previously deployed data. Reflashing an SSD overwrites its application data, so back up before rebuilding it.
