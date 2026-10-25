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
