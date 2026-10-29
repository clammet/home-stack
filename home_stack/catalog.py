"""Create a Raspberry Pi Imager catalogue for a privately hosted image."""
import argparse
from datetime import date
import hashlib
import json
import lzma
from pathlib import Path
from urllib.parse import urlsplit


def entry(image, url, release_date, icon_url, allow_http=False):
    for value in (url, icon_url):
        parsed = urlsplit(value)
        if parsed.scheme not in (("https", "http") if allow_http else ("https",)) or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Image and icon URLs require HTTPS (or explicit --allow-http), without embedded credentials")
    date.fromisoformat(release_date)
    if not image.is_file() or not (image.name.endswith(".img") or image.name.endswith(".img.xz")):
        raise ValueError("Expected a regular .img or .img.xz file")
    digest, size = hashlib.sha256(), 0
    opener = lzma.open if image.suffix == ".xz" else open
    with opener(image, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if not size:
        raise ValueError("Image is empty")
    return {"name": "Home Stack — MicroOS (Pi 5)",
            "description": "Self-hosted home hub with unattended updates; validate this image on your NVMe HAT before deployment",
            "icon": icon_url, "url": url, "extract_size": size, "extract_sha256": digest.hexdigest(),
            "image_download_size": image.stat().st_size, "release_date": release_date,
            "devices": ["pi5"], "init_format": "none"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--url", required=True, help="Final URL of this exact image file (HTTPS by default)")
    parser.add_argument("--icon-url", required=True, help="URL of your catalogue icon (HTTPS by default)")
    parser.add_argument("--release-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=Path("dist/os_list.json"))
    parser.add_argument("--allow-http", action="store_true", help="Allow plaintext URLs for a trusted local network")
    args = parser.parse_args()
    try:
        result = {"imager": {"devices": [{"name": "Raspberry Pi 5", "description": "Pi 5 with NVMe HAT", "tags": ["pi5"]}]},
                  "os_list": [entry(args.image, args.url, args.release_date, args.icon_url, args.allow_http)]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        print(f"Created {args.output}; no files were uploaded")
    except (ValueError, OSError, lzma.LZMAError) as error:
        parser.exit(1, f"Catalogue failed: {error}\n")


if __name__ == "__main__":
    main()
