# Validation record

24 October 2026.

Completed:

- Configuration validation and generated shell syntax checked locally on macOS/Python 3.9. Generated systemd units also passed `systemd-analyze verify` in Linux (only unrelated Ubuntu vendor-unit deprecation notices).
- Standard-library unit tests cover parsed configuration, image geometry/checksums, compressed catalogue metadata, DNS packets, generation acceptance, failed migrations, interrupted deployments, failed recovery, data-copy/pull failures, cleanup and persistent faults.
- All 25 local tests passed. The three Linux disk tests passed as root in a temporary Ubuntu 26.04 ARM64 Lima VM: GPT and MBR assembly, and an ext4 capacity regression check for an 8 GiB archive payload. They resized Btrfs, preserved boot/root identity and copied two application archives outside Combustion's in-memory payload. VM deletion completed successfully.
- Full assembly of a 32 GiB experimental upstream MicroOS image with both real ARM64 containers completed. The macOS wrapper verified the transferred image's SHA-256 and deleted the temporary VM. The output and nonsecret manifest are in `dist/`; a generated private test configuration is in `build/validation.json`. This validates assembly, not Pi boot. That build used an explicit `eth0` setting; use the current example's automatic wired-interface selection for a new deployment build.
- After adding the macOS monitor and local server, 34 local tests pass. These include actual localhost HTTP requests, byte-range resumption, compressed-image validators, rejected traversal/unlisted files, and changed-image detection. Monitor tests exercise DNS-answer parsing, consecutive failures, recovery, notification retries and sleep gaps. Installation/reinstallation/removal are tested in temporary directories with native `plutil`; launchd and desktop notifications are mocked, so no real monitor was installed during testing.

Not established by these tests:

- Physical Raspberry Pi 5 / NVMe HAT boot.
- Full MicroOS first boot, SELinux behaviour, network/firewall reachability and Home Assistant integrations on the target.
- Successful and failed transactional OS update/reboot on hardware.
- Embedded Imager's custom-catalogue behaviour or writing an SSD by Network Install.
- Actual desktop notification permission/banner delivery and a real LaunchAgent's login lifecycle on the target Mac; use the monitor's `test-notification` after installation.

Unit tests simulate container execution to inject failure and power-loss states. Disk integration tests use synthetic MicroOS/AArch64 markers, not a Pi boot. Do not interpret either as a hardware-certified appliance release.

Use the [Pi 5 acceptance checklist](pi5-boot.md) before deployment.

## Immich support — 29 October 2026

Local validation covers Immich configuration parsing, matching image tags and explicit pins, optional JSON seeds and transfer, reserved environment values, port/dependency-name collisions, all four ARM64 archive build inputs, private dependency networking, credential handling and first-run health checks without an account. Simulated deployments cover dependency-only updates, partial pull failures, interrupted upgrades, group rollback with media/database preservation, initial failure cleanup, missing-data refusal and cross-application image retention.

Both the default configuration and the synthetic configuration directory render successfully. Generated shell scripts pass `bash -n`, and the rendered standalone controller imports its bundled helpers successfully. The optional Immich configuration example also loads and renders. The local test suite has 72 tests: 69 pass and the three opt-in Linux disk integration tests are skipped.

These checks mock container execution; no live Immich onboarding, upload, processing, database migration or Podman/SELinux acceptance on the Pi has been established. The earlier complete disk-image build predates Immich support. Follow the [Immich acceptance steps](immich.md#storage-and-hardware) before relying on a new installation.

## UniFi support — 29 October 2026

Local validation covers optional UniFi configuration, binary `.unf` resolution and macOS transfer, confined paths and file-size bounds, managed environment values, port collisions and coexistence with OpenCloud, optional guest portal ports, and ARM64 archive inputs. Runtime tests verify exact multipart backup bytes, setup API response parsing, refusal to restore over an existing administrator, restart/readiness checks, private error handling, initial failure cleanup, recovery after power loss during restore submission, and preservation of restored database/UI edits during updates.

The default example, synthetic configuration directory, and an enabled UniFi/OpenCloud configuration with a synthetic backup render successfully. All generated scripts pass `bash -n`, and each rendered standalone controller imports its bundled helpers. The full local suite has 99 tests: 96 pass and the three opt-in Linux disk integration tests are skipped. The existing localhost HTTP test requires socket access outside the restricted execution sandbox.

Container execution and restoration are simulated. No local Docker daemon was running, and no real UniFi backup, device migration, Pi/MicroOS deployment or live update/rollback was tested. The earlier complete image build predates UniFi support. The internal setup API and selected image's embedded MongoDB/restart contract require commissioning with a compatible image and a real backup, as described in [UniFi validation](unifi.md#updates-and-validation).

## OpenCloud replacement — 1 November 2026

OpenCloud replaces the Seafile preset, dependencies, runtime, examples and tests. The official production image's manifest includes Linux/ARM64. Local validation covers configuration and secret transfer, HTTPS origins, direct/reverse-proxy TLS settings, managed environment values, port collisions and UniFi coexistence, and one ARM64 archive input. Runtime tests exercise explicit initialization with private credentials, stable credentials across retries, UID/GID ownership setup, preservation of both persistent mounts, missing-data refusal, persisted URL/issuer checks, and configuration/file/credential recovery after failed or interrupted updates. Updates do not rerun initialization.

The default example, synthetic configuration directory, and a configuration enabling both UniFi and OpenCloud render successfully. Generated shell scripts pass `bash -n`, and the standalone rendered controllers import their helpers. The full local suite has 99 tests: 96 pass and the three opt-in Linux disk integration tests are skipped. The localhost HTTP test needs socket access outside the restricted execution sandbox.

No Docker daemon is running locally. Container execution is simulated, so live OpenCloud first login, file transfers, desktop sync, reverse proxy routing, Podman/SELinux behavior and real update/rollback on the Pi remain unverified. The earlier complete image build predates OpenCloud. Follow the [OpenCloud acceptance steps](opencloud.md#storage-and-acceptance) on the target. Existing Seafile data is neither migrated nor deleted by this repository change.
