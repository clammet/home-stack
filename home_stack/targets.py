"""Pi 5 is ARM64; CPU compatibility alone does not prove SSD boot support."""
UPSTREAM_BASE = "https://download.opensuse.org/ports/aarch64/tumbleweed/appliances/openSUSE-MicroOS.aarch64-ContainerHost-RaspberryPi.raw.xz"


def validate_elf(header):
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        raise ValueError("Base must contain little-endian AArch64 binaries")


def validate_container(config):
    if config.get("os") != "linux" or config.get("architecture") != "arm64":
        raise ValueError("Bundled containers must be linux/arm64")
