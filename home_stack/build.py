"""Linux-only raw image assembly. Never opens a user-supplied block device."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse

from .config import load
from .provision import render
from .targets import UPSTREAM_BASE, validate_elf, validate_container

MIB = 1024 * 1024


def run(*args, input=None):
    result = subprocess.run([str(a) for a in args], input=input, text=True, check=True, stdout=subprocess.PIPE)
    return result.stdout.strip()


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_text(text):
    # Accept both GNU and BSD checksum formats; reject ambiguous responses.
    hashes = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", text)
    if len(hashes) != 1:
        raise ValueError("Expected exactly one SHA-256 checksum")
    return hashes[0].lower()


def download(url, path):
    if not url.startswith("https://"):
        raise ValueError("Downloads require HTTPS")
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, Path(path).open("wb") as output:
        if not response.url.startswith("https://"):
            raise ValueError("Refusing an HTTPS downgrade")
        shutil.copyfileobj(response, output, 8 * MIB)


def layout(table, total_mib, seed_mib):
    """Calculate a non-shrinking root extension and an aligned final seed."""
    if table.get("sectorsize", 512) != 512 or table["label"] not in ("gpt", "dos"):
        raise ValueError("Expected GPT/MBR with 512-byte sectors")
    parts = table["partitions"]
    numbers = [int(re.search(r"(\d+)$", p["node"]).group(1)) for p in parts]
    if not parts or numbers != list(range(1, len(parts) + 1)):
        raise ValueError("Expected contiguous partition numbers")
    if table["label"] == "dos" and (len(parts) >= 4 or any(p["type"].lower() in ("5", "f", "85") for p in parts)):
        raise ValueError("No free MBR primary partition slot")
    if parts != sorted(parts, key=lambda p: p["start"]):
        raise ValueError("Expected partitions in physical order")
    if any(a["start"] + a["size"] > b["start"] for a, b in zip(parts, parts[1:])):
        raise ValueError("Overlapping base partitions")
    # Leave 1 MiB at end for backup GPT, even for DOS images.
    seed_start = (total_mib - seed_mib - 1) * 2048
    root = parts[-1]
    if seed_start < root["start"] + root["size"]:
        raise ValueError("image_size_mib would shrink the base filesystem; use a larger image")
    return len(parts), seed_start - root["start"], seed_start, seed_mib * 2048


def partition_script(dump, table, total_mib, seed_mib):
    number, root_size, seed_start, seed_size = layout(table, total_mib, seed_mib)
    lines = []
    root_node = table["partitions"][-1]["node"]
    for line in dump.splitlines():
        if line.startswith("last-lba:"):
            continue  # sfdisk recalculates this for the extended image
        if line.startswith(root_node + " ") or line.startswith(root_node + ":"):
            line, changed = re.subn(r"size=\s*\d+", f"size={root_size}", line)
            if changed != 1:
                raise ValueError("Cannot identify root partition size")
        lines.append(line)
    kind = "0FC63DAF-8483-4772-8E79-3D69D8477DE4" if table["label"] == "gpt" else "83"
    node = re.sub(r"\d+$", str(number + 1), root_node)
    lines.append(f"{node} : start={seed_start}, size={seed_size}, type={kind}")
    return "\n".join(lines) + "\n"


@contextmanager
def loop_device(image):
    device = run("losetup", "--find", "--show", "--partscan", image)
    try:
        run("udevadm", "settle")
        yield device
    finally:
        run("losetup", "--detach", device)


@contextmanager
def mounted(device, path, options):
    path.mkdir()
    run("mount", "-o", options, device, path)
    try:
        yield path
    finally:
        run("umount", path)


def seed_size_mib(payload_bytes):
    # ext4's inode tables, allocation bitmaps and journal grow with its size.
    # A fixed allowance fits Pi-hole alone but not multi-GiB HA archives.
    return max(512, (payload_bytes + payload_bytes // 8 + 256 * MIB + MIB - 1) // MIB)


def assemble(raw, combustion, total_mib, workspace, archives):
    table = json.loads(run("sfdisk", "--json", raw))["partitiontable"]
    payload_bytes = sum(p.stat().st_size for p in archives.iterdir()) + sum(p.stat().st_size for p in combustion.rglob("*") if p.is_file())
    seed_mib = seed_size_mib(payload_bytes)
    number, _, _, _ = layout(table, total_mib, seed_mib)
    # Inspect BEFORE rewriting partition boundaries. Require the official layout
    # shape and matching MicroOS root architecture, not an arbitrary disk image.
    with loop_device(raw) as device:
        root = f"{device}p{number}"
        if run("blkid", "-s", "TYPE", "-o", "value", root) != "btrfs":
            raise ValueError("Last base partition must be the MicroOS Btrfs root")
        with mounted(root, workspace / "inspect", "ro") as mount:
            release = (mount / "usr/lib/os-release").read_text()
            if not re.search(r'^ID="?(?:opensuse-)?microos"?$', release, re.MULTILINE):
                raise ValueError("Base is not openSUSE MicroOS")
            with (mount / "usr/bin/bash").open("rb") as stream:
                elf = stream.read(20)
            validate_elf(elf)
    dump = run("sfdisk", "--dump", raw)
    script = partition_script(dump, table, total_mib, seed_mib)
    with raw.open("r+b") as stream:
        stream.truncate(total_mib * MIB)
    run("sfdisk", "--no-reread", "--no-tell-kernel", raw, input=script)
    with loop_device(raw) as device:
        with mounted(f"{device}p{number}", workspace / "resize", "rw,subvolid=5") as mount:
            run("btrfs", "filesystem", "resize", "max", mount)
        seed = f"{device}p{number + 1}"
        run("mkfs.ext4", "-q", "-L", "combustion", "-m", "0", seed)
        with mounted(seed, workspace / "seed", "rw,nosuid,nodev,noexec") as mount:
            shutil.copytree(combustion, mount / "combustion")
            shutil.copytree(archives, mount / "archives")
            os.sync()
    run("sfdisk", "--verify", raw)


def requirements():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Full image builds require root on Linux. Use --render-only to generate/inspect provisioning on any platform.")
    required = ("sfdisk", "losetup", "udevadm", "mount", "umount", "blkid", "btrfs", "mkfs.ext4", "skopeo", "systemd-analyze")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise ValueError("Missing build tools: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=JSON", help="Override a dotted config key with a JSON value")
    parser.add_argument("--output", type=Path, default=Path("dist/home-stack-pi5.img"))
    parser.add_argument("--base-image", type=Path, help="Existing official .raw/.img or .xz image")
    parser.add_argument("--base-url", help="Override the target-specific official image URL")
    parser.add_argument("--base-sha256", help="Pinned SHA-256 of compressed/downloaded input (required for local images)")
    parser.add_argument("--render-only", type=Path, metavar="DIRECTORY", help="Generate provisioning only; no downloads, Linux, or root required")
    parser.add_argument("--experimental-upstream", action="store_true", help="Use unvalidated upstream Pi image; SSD boot is not guaranteed")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = load(args.config, args.set)
        if args.render_only:
            if args.render_only.exists():
                raise ValueError("Render directory already exists; choose a new directory")
            render(config, args.render_only)
            print(f"Provisioning rendered to {args.render_only}; no disk image was built.")
            return
        requirements()
        if sum(bool(v) for v in (args.base_image, args.base_url, args.experimental_upstream)) != 1:
            raise ValueError("Choose --base-image/--base-url with an SSD-validated Pi 5 MicroOS base, or --experimental-upstream for hardware testing; see docs/pi5-boot.md")
        if (args.base_image or args.base_url) and not args.base_sha256:
            raise ValueError("Custom base inputs require a trusted --base-sha256")
        base_url = args.base_url or UPSTREAM_BASE
        for key in ("container_calendar", "os_calendar"):
            if config["updates"][key]:
                run("systemd-analyze", "calendar", config["updates"][key])
        output = args.output.resolve()
        for suffix in (".img", ".img.sha256", ".img.json"):
            path = args.output.with_suffix(suffix)
            if path.exists() or path.is_symlink():
                raise ValueError("Output exists; choose a new filename (devices are never valid outputs)")
        if output.suffix != ".img":
            raise ValueError("Output must end in .img")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".home-stack-build-", dir=output.parent) as tmp:
            workspace = Path(tmp)
            base = args.base_image
            expected = args.base_sha256
            if base:
                if not base.is_file() or not expected:
                    raise ValueError("Local base-image requires a regular file and --base-sha256")
            else:
                base = workspace / ("base.raw.xz" if urllib.parse.urlparse(base_url).path.endswith(".xz") else "base.raw")
                if not expected:
                    sums = workspace / "base.sha256"
                    download(base_url + ".sha256", sums)
                    expected = checksum_text(sums.read_text())
                download(base_url, base)
            expected = checksum_text(expected)
            actual = checksum(base)
            if actual != expected:
                raise ValueError("Base SHA-256 mismatch. The upstream alias may have changed; retry or pin a snapshot URL and checksum.")
            raw = workspace / "image.raw"
            opener = lzma.open if base.suffix == ".xz" else open
            print("Unpacking base image", flush=True)
            with opener(base, "rb") as source, raw.open("xb") as target:
                shutil.copyfileobj(source, target, 8 * MIB)
            combustion = workspace / "combustion"
            render(config, combustion)
            archives = workspace / "archives"
            archives.mkdir()
            containers = {}
            for app in config["apps"]:
                if not app["enabled"]:
                    continue
                archive = archives / (app["name"] + ".tar")
                print(f"Fetching linux/arm64 container: {app['name']}", flush=True)
                run("skopeo", "copy", "--override-os", "linux", "--override-arch", "arm64",
                    "docker://" + app["image"],
                    f"docker-archive:{archive}:localhost/home-stack-{app['name']}:build")
                validate_container(json.loads(run("skopeo", "inspect", "--config", f"docker-archive:{archive}")))
                containers[app["name"]] = {"reference": app["image"], "archive_sha256": checksum(archive)}
            assemble(raw, combustion, config["image_size_mib"], workspace, archives)
            image_sum = checksum(raw)
            metadata = workspace / "image.img.json"
            metadata.write_text(json.dumps({
                "target": "pi5", "platform": "linux/arm64", "base_sha256": actual,
                "experimental_upstream": args.experimental_upstream,
                "hardware_validated": False, "containers": containers,
                "image_sha256": image_sum, "image_size_mib": config["image_size_mib"]}, indent=2) + "\n")
            sums = workspace / "image.img.sha256"
            sums.write_text(f"{image_sum}  {output.name}\n")
            published = []
            try:
                for source, dest in ((metadata, output.with_suffix(".img.json")),
                                     (sums, output.with_suffix(".img.sha256")), (raw, output)):
                    os.link(source, dest)
                    published.append(dest)
            except OSError:
                for dest in published:
                    dest.unlink()
                raise
            print(f"Built {output} ({config['image_size_mib']} MiB); Pi 5 SSD boot requires hardware validation")
    except (ValueError, OSError, subprocess.CalledProcessError, lzma.LZMAError) as error:
        parser.exit(1, f"Build failed: {error}\n")
