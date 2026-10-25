"""Serve one built image and a generated Imager catalogue on a trusted LAN."""
import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import lzma
import os
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import urlsplit

from .catalog import entry
from .build import checksum

ICON = b'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"><rect width="128" height="128" rx="24" fill="#19352b"/><path d="M24 62 64 28l40 34v42H76V76H52v28H24Z" fill="#8ce0b5"/></svg>'''


@dataclass
class Resource:
    content: object  # bytes or a single explicitly selected Path
    content_type: str
    etag: str
    size: int
    fingerprint: tuple = ()


def fingerprint(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def byte_range(header, size):
    """Parse one HTTP byte range; return inclusive start/end, or raise."""
    if header is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or size == 0:
        raise ValueError("Unsupported range")
    first, last = match.groups()
    if not first:
        if not last or int(last) == 0:
            raise ValueError("Invalid suffix range")
        return max(0, size - int(last)), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end


def detect_address():
    # UDP connect only asks the routing table for a source address; no datagram
    # is sent. --bind is available for VPNs, multiple interfaces, or offline LANs.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]


def load_token(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Token must be a regular file")
        token = path.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ValueError("Invalid saved server token")
        return token
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
        return token


def resources(image, base_url, release_date):
    before = fingerprint(image)
    image_name = "image.img.xz" if image.name.endswith(".img.xz") else "image.img"
    item = entry(image, base_url + "/" + image_name, release_date, base_url + "/icon.svg", allow_http=True)
    representation_hash = checksum(image) if image_name.endswith(".xz") else item["extract_sha256"]
    if fingerprint(image) != before:
        raise ValueError("Image changed while computing its hash")
    catalogue = json.dumps({"imager": {"devices": [{"name": "Raspberry Pi 5", "description": "Pi 5", "tags": ["pi5"]}]},
                            "os_list": [item]}, indent=2).encode() + b"\n"
    prefix = urlsplit(base_url).path
    result = {prefix + "/" + image_name: Resource(image, "application/octet-stream", '"' + representation_hash + '"', before[2], before)}
    # The checksum is for the uncompressed disk image, like Imager's field.
    for name, data, mime in (("os_list.json", catalogue, "application/json"), ("icon.svg", ICON, "image/svg+xml"),
                             ("image.img.sha256", (item["extract_sha256"] + "  image.img\n").encode(), "text/plain")):
        result[prefix + "/" + name] = Resource(data, mime, '"' + hashlib.sha256(data).hexdigest() + '"', len(data))
    return result


def make_handler(allowed):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # Don't persist image URLs containing the access token.

        def do_HEAD(self):
            self.serve(head=True)

        def do_GET(self):
            self.serve(head=False)

        def serve(self, head):
            resource = allowed.get(urlsplit(self.path).path)
            if resource is None:
                self.send_error(404)
                return
            stream = None
            try:
                if isinstance(resource.content, Path):
                    if fingerprint(resource.content) != resource.fingerprint:
                        self.send_error(409, "Image changed; restart the server")
                        return
                    stream = resource.content.open("rb")
                    stat = os.fstat(stream.fileno())
                    opened = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    if opened != resource.fingerprint:
                        self.send_error(409, "Image changed; restart the server")
                        return
                requested = self.headers.get("Range")
                # A mismatched validator requests the complete current resource.
                if self.headers.get("If-Range") not in (None, resource.etag):
                    requested = None
                try:
                    start, end = byte_range(requested, resource.size)
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{resource.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206 if requested else 200)
                self.send_header("Content-Type", resource.content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", resource.etag)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if requested:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{resource.size}")
                self.end_headers()
                if head:
                    return
                if stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            self.close_connection = True
                            return
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                else:
                    self.wfile.write(resource.content[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except OSError:
                self.close_connection = True
            finally:
                if stream:
                    stream.close()

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="The .img or .img.xz to serve; no directory is exposed")
    parser.add_argument("--bind", help="Mac's LAN IPv4 address; default: routing-table address")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token-file", type=Path, default=Path("build/image-server.token"), help="Saved private URL token, reused across server restarts")
    args = parser.parse_args()
    try:
        bind = str(ipaddress.IPv4Address(args.bind or detect_address()))
        address = ipaddress.IPv4Address(bind)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Bind to a specific LAN address or 127.0.0.1")
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        image = args.image.resolve(strict=True)
        token = load_token(args.token_file)
        base_url = f"http://{bind}:{args.port}/{token}"
        release_date = datetime.fromtimestamp(image.stat().st_mtime).date().isoformat()
        print("Hashing the image for Imager verification; large raw images can take a moment.", flush=True)
        allowed = resources(image, base_url, release_date)
        with ThreadingHTTPServer((bind, args.port), make_handler(allowed)) as server:
            print(f"Catalogue: {base_url}/os_list.json", flush=True)
            print(f"Desktop Imager: rpi-imager --repo {base_url}/os_list.json", flush=True)
            print("HTTP is plaintext: use a trusted LAN. Only this image, its checksum, icon and catalogue are served.", flush=True)
            print("Keep this process running and the Mac awake during installation. Ctrl-C stops it.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print("Image server stopped.")
    except (ValueError, OSError, lzma.LZMAError) as error:
        parser.exit(1, f"Image server failed: {error}\n")


if __name__ == "__main__":
    main()
