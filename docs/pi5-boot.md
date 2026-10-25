# Raspberry Pi 5 NVMe boot qualification

Research checked 24 October 2026. Target: Raspberry Pi 5 with an SSD on a PCIe/NVMe HAT, wired Ethernet.

The boot chain is EEPROM → FAT boot files/U-Boot → GRUB → MicroOS kernel/root. The user's Pi already boots HAOS from its NVMe HAT, which establishes that the EEPROM, HAT and SSD can boot that OS. MicroOS uses a different boot chain: this does not establish that its U-Boot can load the kernel from NVMe.

openSUSE's [Pi 5 announcement](https://news.opensuse.org/2025/11/04/raspberrypi5-opensuse/) documents Tumbleweed/MicroOS support and distinguishes SD boot from incomplete U-Boot disk/USB support. Upstream work has continued: a [September 2026 U-Boot patch series](https://www.mail-archive.com/u-boot%40lists.u-boot-project.org/msg06878.html) covers NVMe and USB support. A proposed patch series is not evidence that an arbitrary MicroOS image contains those changes.

The user supplied a saved copy of the [openSUSE Pi 5 hardware page](https://en.opensuse.org/HCL:Raspberry_Pi5), revision 210075, last edited 29 September 2026. Its known-issues list explicitly limits bootable media to microSD and lists missing USB/network support in U-Boot and GRUB. Thus stock openSUSE NVMe boot is a documented limitation, not merely an untested EEPROM setting. The experimental image built here should not replace the working HAOS install as though NVMe support were established.

The same page reports an Ethernet freeze during bulk transfers and gives `ethtool -K end0 tso off gso off ufo off lro off` as a temporary workaround. That workaround was not automatically enabled or hardware-tested here. Network configuration now defaults to matching wired Ethernet without assuming `eth0`; an explicit interface can be selected when required. The previously listed serial-debug-device boot workaround is crossed out in that revision.

The builder therefore offers two explicit paths:

- Supply a trusted checksum and a Pi 5/NVMe-qualified MicroOS base with `--base-image` or `--base-url`.
- Use `--experimental-upstream` to assemble the current generic Raspberry Pi image for testing. This flag is not a claim that it boots from NVMe.

The builder verifies MicroOS identity, AArch64 ELF architecture, partition geometry and Btrfs root. It preserves the base boot partitions and does not splice in a third-party U-Boot, disable signature checking, or alter EEPROM. Its manifest always reports `hardware_validated: false`; supplying a base is not automated hardware qualification.

Build from the pristine, unbooted download whose copy you tested. A disk captured after MicroOS has completed first boot may no longer invoke Combustion, and is not a suitable base without explicitly resetting its first-boot state.

Before treating an image as an unattended appliance:

1. Record Pi revision, EEPROM version, HAT model, SSD model/firmware, base checksum, U-Boot and kernel versions.
2. Boot that base directly from NVMe with the SD slot empty; exercise Ethernet and both warm/cold boot.
3. Boot the assembled image and complete first-boot provisioning without console commands.
4. Verify persistence, DNS UDP/TCP, Home Assistant discovery, and any USB/Bluetooth devices you use.
5. Exercise a successful container update and an intentionally failing candidate; verify both old image and old database return.
6. Exercise an OS update/reboot and recoverable health failure. Separately test what happens if firmware cannot load the kernel; userspace rollback cannot repair that case.
7. Test power interruption during deployment and observe cooldown, recovery and fault notification.

Raspberry Pi documents [NVMe boot configuration](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#nvme-ssd-boot). EEPROM boot mode `6` is NVMe; boot order is read right to left. For example, `0xf16` tries NVMe, SD, then repeats. Apply bootloader changes using the official tools from a working system; this builder does not flash firmware.

The remaining release blocker is a qualified Pi 5 NVMe base and a recorded hardware run. A VM can verify disk assembly and userspace behaviour, but cannot establish RP1/PCIe/NVMe boot compatibility.
