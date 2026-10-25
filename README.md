# Home Stack

Build an ARM64 openSUSE MicroOS appliance for a Raspberry Pi 5 with an NVMe HAT. JSON selects the initial containers; the supplied presets deploy Home Assistant Container and Pi-hole. Application data and container storage live on SSD. OS and container updates run unattended, with health checks, application data rollback, and optional problem notifications.

**Status: experimental.** A complete image build and the automated tests pass. The supplied September 2026 openSUSE hardware documentation still lists SD-only boot; the stock MicroOS image must not be treated as NVMe-ready. Your working HAOS NVMe installation establishes the hardware/EEPROM path, but uses a different OS boot chain. See [Pi 5 boot support](docs/pi5-boot.md). This repository does not yet supply a hardware-qualified NVMe base image.

## Build

Python 3.9+ is required. Start with:

```sh
cp config.example.json config.json
chmod 600 config.json
```

Replace the example Pi-hole password. Add your SSH public key for recovery access and, optionally, an HTTPS webhook for fault notifications. Use a router DHCP reservation, or configure a static IPv4 address and gateway. The host's DNS servers must remain independent of this Pi-hole instance. Wired Ethernet is the initial supported network configuration.

On macOS, install Lima (`brew install lima`), then build with a MicroOS Raspberry Pi base that has been validated on your exact Pi 5/NVMe combination:

```sh
python3 build-macos.py --config config.json \
  --base-image downloads/microos-pi5-nvme.raw.xz \
  --base-sha256 '<trusted SHA-256 of the compressed input>'
```

The wrapper creates a temporary Linux VM, copies only build inputs, verifies the returned image, then deletes the VM on success or failure. It does not mount host directories or reuse existing VMs. Allow roughly 100 GiB of working space; the default VM has 4 CPUs and 4 GiB RAM. A host crash or forced process termination can leave a VM behind; the build prints its name for cleanup. Cached Linux downloads and final output remain.

For testing the moving upstream Raspberry Pi base, explicitly opt into its unvalidated boot status:

```sh
python3 build-macos.py --config config.json --experimental-upstream
```

On Linux, use root with these tools installed (Debian/Ubuntu package names):

```sh
sudo apt-get install python3 tzdata skopeo util-linux fdisk udev btrfs-progs e2fsprogs systemd
sudo python3 build.py --config config.json \
  --base-image downloads/microos-pi5-nvme.raw.xz \
  --base-sha256 '<trusted SHA-256 of the compressed input>'
```

Linux also accepts `--experimental-upstream`. A custom `--base-url` requires a trusted `--base-sha256`. The experimental download checks the checksum published over HTTPS; this is integrity checking, **not independent verification of the upstream GPG signature**. Verify signed downloads separately before trusting a pinned base.

Output is `dist/home-stack-pi5.img`, a SHA-256 file, and a nonsecret manifest of base/container inputs. Output files are never overwritten, and the builder only partitions temporary regular files through loop devices. Both ARM64 container images are bundled. First boot still needs internet access to install signed MicroOS packages through Combustion.

The default raw image is 32 GiB. Its root is expanded during assembly; the final seed partition caps usable space at the configured image size. A larger SSD's remaining space is unused. Set `image_size_mib` before building to allocate more space, leaving enough working space for downloads, image copies, and future application snapshots. A nominal 32 GB drive is smaller than this default image; use a larger SSD.

Write the image with Raspberry Pi Imager's **Use custom** option, without Raspberry Pi OS customization. This builder never writes a physical disk. See [network installation](docs/network-install.md) for installing directly to NVMe without an SD card.

After first boot, open Pi-hole at `http://<pi-address>/admin/` and Home Assistant at `http://<pi-address>:8123/`. Complete Home Assistant onboarding and configure your integrations once. This is Home Assistant Container, without the Home Assistant OS/Supervisor app store. It uses host networking and privileged access for hardware, following the upstream container deployment model; Podman compatibility still needs appliance validation. [Home Assistant installation](https://www.home-assistant.io/installation/linux/)

After verifying DNS, configure your router to advertise the Pi as the clients' DNS server. A public secondary DNS server can bypass blocking even while Pi-hole is healthy; it is not guaranteed to be used only during failure.

## Configuration

`config.example.json` contains the initial configuration. Unknown keys, malformed types, multiline environment values, duplicate app names, and known host port collisions are rejected. Defaults fill omitted settings. `--set 'updates.os_calendar=null'` overrides dotted object keys; replace the `apps` array as a whole to override applications.

| Setting | Behaviour |
| --- | --- |
| `apps` | Array of deployments; `name` must be unique and `kind` is `pihole`, `homeassistant`, or `container` |
| `apps[].image` | Fully qualified image reference; use a moving tag for automatic releases or a digest to pin |
| `apps[].enabled` | Defaults to true; disabled apps are not bundled or deployed |
| `apps[].auto_update` | Defaults to true; false retains boot/recovery checks but disables automatic image pulls |
| `apps[].environment` | Additional string environment values; managed preset values cannot be overridden |
| `apps[].settings` | Pi-hole password, HTTP port, upstream DNS, DNSSEC, domain-name forwarding policy, cache optimizer, web session timeout, optional initial adlists and local DNS records |
| `network` | Wired interface (`null` selects Ethernet automatically; explicit names such as `end0` also work), optional static IPv4 address/gateway, independent host DNS |
| `updates` | Container/OS systemd calendars, jitter, health probation, failed-image cooldown |
| `logging` | RAM-only or no host journal; default severity is `crit` |
| `notifications.webhook_url` | Optional HTTPS endpoint receiving JSON on faults and recovery; no routine healthy notifications |
| `ssh_authorized_keys` | Root public keys; no password login, SSH disabled when empty |

A generic application needs an explicit host-accessible health check and one managed data directory. For example, add an entry shaped like this, substituting an actual ARM64 image:

```json
{
  "name": "extra-service",
  "kind": "container",
  "image": "ghcr.io/your-org/your-service:stable",
  "network": "bridge",
  "ports": ["8080:80/tcp"],
  "data_path": "/data",
  "environment": {"MODE": "production"},
  "health": {"kind": "http", "url": "http://127.0.0.1:8080/"}
}
```

TCP health uses `{"kind":"tcp","port":8080}`. Generic containers can opt into host networking or privileges. Extra volumes/devices, multiple data mounts and arbitrary health shell commands are intentionally not exposed. For host networking, only the health port is opened by the generic preset; other protocol/firewall requirements need a dedicated preset. Keep all migration-sensitive data in the managed directory. Preset health/mount contracts are fixed; only one enabled Pi-hole and one enabled Home Assistant are supported.

Adlists seed new Pi-hole data only; subsequent UI changes survive updates. Pi-hole environment settings remain authoritative. Home Assistant keeps configuration, integration state and recorder history on SSD. Its initial YAML limits application logging to warnings; unlike Pi-hole's logs, its own files can persist in its data directory.

Pi-hole settings also accept `dnssec` and `domain_needed` booleans (both default to false). `domain_needed` prevents forwarding queries for plain names without a domain to upstream DNS. `cache_optimizer` limits how many seconds stale cache entries may be served while refreshing them (default 3600; -1 disables this, 0 allows unlimited age). `web_session_timeout` is in seconds (default 1800). These map to [Pi-hole's FTL settings](https://docs.pi-hole.net/ftldns/configfile/).

## Updates and recovery

Containers check daily at 03:00; MicroOS checks Sundays at 04:00, in the configured timezone, with up to ten minutes of jitter. Timers catch up after missed runs. OS updates reboot after successful installation, so these are start times rather than hard maintenance windows. A shared lock serializes OS maintenance and application deployment.

For each container, the controller pulls before stopping the service, stops cleanly, copies its data using reflinks where available, starts the new image against the copy, and requires consecutive successful probes. Pi-hole checks local DNS over both UDP and TCP plus its web page; Home Assistant checks its local HTTP page. Acceptance retains the old image and data as one rollback generation. Failure or interrupted deployment restores that pair. Normal recovery runs every five minutes; rejected image IDs have a 24-hour cooldown.

These probes test service availability, not every integration or automation. A later rollback can lose changes made since the retained data snapshot. Local rollback copies are not backups against SSD failure; arrange an independent backup for Home Assistant data. An update briefly interrupts the updated service, and an OS reboot interrupts both. A single Pi cannot provide uninterrupted DNS through reboots.

MicroOS transactional updates and health-checker provide OS recovery once Linux boots far enough to run health checks. They cannot guarantee recovery from EEPROM, U-Boot, FAT boot partition, power, or SSD failures. [MicroOS architecture](https://get.opensuse.org/microos/)

Controller code and provisioning are bundled into the image and are **not fetched from Git automatically**. OS packages and configured container releases update themselves; changes to this project's controller/configuration currently require an image rebuild or an explicit maintenance deployment. Do not assume upstream container updates can always preserve every Home Assistant integration: incompatible releases are problems requiring attention.

Reflashing an existing SSD overwrites its application data. Back up and restore that data, or perform a deliberate in-place maintenance deployment when changing the controller/configuration.

## Low-log policy

Pi-hole follows the reference `pihole-builder` configuration: query logging off, privacy level 3, zero query history, no history import or restored sessions, and its FTL database in shared memory. Bounded RAM mounts hold Pi-hole logs and temporary files; stdout/stderr collection is disabled. Gravity, settings and blocklists persist on SSD, as do container writable layers and metadata. No explicit “save RAM data” step is needed.

The host journal defaults to a 16 MiB volatile journal retaining only critical/alert/emergency messages. Forwarding, syslog/audit daemons and disk core dumps are disabled. SELinux enforcement is retained. To keep ordinary errors in RAM, set `logging.max_level` to `err`. This is a privacy/low-log appliance, not a disk-write-free host.

Problems are recorded durably in the controller's `status.json` independently of logs. The optional webhook receives `hostname`, `component`, `status`, `message`, and a Unix `time`; it must accept JSON POSTs and return 2xx. Delivery retries during reconciliation; changed states are coalesced per component. No endpoint is configured by default, so there are **no remote alerts until you configure one**. A dead/offline host cannot notify; monitor it externally if required.

On the Pi, as root:

```sh
python3 /etc/home-stack/controller.py status
systemctl start home-stack-update.service
systemctl start home-stack-os-update.service
```

Config, environment files and images contain credentials. Keep them private. Seed credentials are removed after successful first boot; this is not secure erasure, and the appliance still needs its runtime secrets. Never publish a personalized image to a public bucket.

## macOS monitoring and local installation server

Install a per-user Pi-hole checker with built-in macOS tools:

```sh
bash scripts/macos/pihole-monitor.sh install --server 192.168.1.2
bash scripts/macos/pihole-monitor.sh test-notification
```

It checks every minute, alerts after three consecutive DNS failures, and announces recovery once. The supplied personal setup script can invoke it using `PIHOLE_SERVER`; see [macOS monitor instructions](docs/macos-monitor.md).

Serve one built image and its generated Imager catalogue on your trusted LAN:

```sh
python3 serve-image.py dist/home-stack-pi5.img
```

The server prints the repository URL, supports resumed downloads and exposes no directory listing. HTTP is plaintext, so use a trusted LAN and keep the personalized image private. See [network installation](docs/network-install.md) for the full procedure and firmware caveats.

## Validation

```sh
python3 build.py --config config.example.json --render-only build/preview
python3 -m unittest discover -s tests -v
python3 build-macos.py --check-vm
```

Render-only validates configuration and emits provisioning without root/downloads. The VM check exercises actual GPT and MBR partition extension, Btrfs resizing, preserved filesystem UUIDs and seed archive placement. Tests model failed migration, rollback failure, interrupted deployment, failed copy/pull, cooldown and fault persistence. See [validation status](docs/validation.md) for results and the remaining device acceptance checks.

Image assembly, Lima lifecycle, and the generation-recovery approach were adapted from the user's public-domain `pihole-builder` project. There is no runtime dependency on that checkout.
