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

## Seafile support — 29 October 2026

Local validation covers minimal and preconfigured Seafile deployments, secret-file resolution and transfer, generated private credentials, static/DHCP URL selection, reserved environment values, rejected partial shared-volume seeds, port/dependency-name conflicts, and bundling all three ARM64 image inputs. Runtime checks cover database readiness before server startup, private dependency networking, mounted-generation data, missing-data refusal, unauthenticated web/file-server probes, dependency-only updates, failed pulls, and complete file/database/credential restoration after failed or interrupted migrations.

Both example configurations render, the generated shell scripts pass `bash -n`, and the standalone rendered controller imports its bundled helpers. The full local suite has 83 tests: 80 pass and the three opt-in Linux disk integration tests are skipped. The existing localhost HTTP tests require socket access outside the restricted execution sandbox.

Container execution is simulated. No local Docker daemon was running, and live Seafile first login, file transfers, sync, MariaDB migrations, Podman/SELinux behaviour and update/rollback on the Pi remain unverified. The earlier full image build predates Seafile support. Follow the [Seafile acceptance steps](seafile.md#storage-and-acceptance) on the target.
