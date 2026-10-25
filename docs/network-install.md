# Install directly to NVMe over the network

**Yes: the relevant feature is Raspberry Pi Network Install / HTTP boot.** It can load a small installer into RAM and write the OS image to the attached NVMe SSD without an SD card. This is separate from TFTP/PXE-style network boot and does not make the installed OS dependent on a network root filesystem.

Raspberry Pi documents [HTTP boot](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#http-boot), which requires Ethernet and can launch embedded Imager. The firmware downloads a boot filesystem (`boot.img` and `boot.sig`); Imager then downloads an OS disk image. Our `.img` is the latter, not a replacement for the firmware's `boot.img`.

## Suggested route

1. Qualify MicroOS NVMe boot using [the device checklist](pi5-boot.md).
2. Start the local server below, or host the personalized `.img`/`.img.xz` and catalogue on a private HTTPS server reachable by the Pi. Keep the image private: it contains credentials and initial configuration.
3. Start Network Install using an up-to-date Pi 5 EEPROM, Ethernet, keyboard and display; choose the custom catalogue, image and NVMe destination in Imager. The write is destructive and needs an explicit destination selection.
4. Reboot from NVMe; Combustion configures the appliance and starts the bundled containers. Initial Home Assistant account/integration setup remains interactive.

This provides an SD-free install with a guided initial write. It is not a fully unattended bare-metal installer.

The current stock openSUSE boot limitation still applies after installation: network installation cannot make an SD-only U-Boot build boot from NVMe. Your working HAOS NVMe setup establishes the hardware path, not MicroOS compatibility. Preserve that installation until you have a qualified MicroOS boot chain.

## Start a local image server

Run this from the checkout on the Mac with Python 3.9+:

```sh
python3 serve-image.py dist/home-stack-pi5.img
```

For the already-built experimental validation artifact, use `dist/home-stack-pi5-experimental.img` instead. It is a test image with a generated password in `build/validation.json`, no SSH key, and the earlier explicit `eth0` setting; build with your own current config for deployment.

The script determines the Mac's LAN IPv4 address, hashes the image, and prints a catalogue URL and a desktop Imager command. With VPNs or multiple network interfaces, choose the interface address and port explicitly:

```sh
python3 serve-image.py dist/home-stack-pi5.img --bind 192.168.1.10 --port 8080
```

Use the printed `http://…/os_list.json` URL as Imager's custom content repository, or for `IMAGER_REPO_URL` where supported. No separate catalogue/icon upload or web-server installation is needed. The process serves only the selected image, a generated catalogue, a checksum and an icon. It streams files without copying them and supports HTTP byte ranges for resumed downloads. It neither lists directories nor exposes the rest of the repository. A private URL token is saved in ignored `build/image-server.token`; keeping that token file preserves URLs across restarts.

Keep the terminal open and the Mac awake while installing. Allow incoming connections if macOS's firewall prompts, and use a LAN without client isolation. Press Ctrl-C to stop serving. The script does not alter your firewall, configure port forwarding, or register a permanent background service.

**HTTP is technically suitable for a temporary server on a trusted LAN.** It provides neither confidentiality nor server authentication. The random URL prevents casual discovery, but anyone who obtains the URL can download the image, and a network observer can see the transfer. The catalogue's checksum detects corruption; because it arrives over the same HTTP connection, it does not authenticate against an active attacker. Use an isolated/trusted LAN for this, or place an HTTPS reverse proxy in front when that trust is inappropriate. Do not expose this server to the internet.

## Produce the catalogue

The helper reads the real image, including decompression when needed, and computes its extracted size and hash. It never uploads anything:

```sh
xz -T0 -k dist/home-stack-pi5.img
python3 -m home_stack.catalog dist/home-stack-pi5.img.xz \
  --url https://images.example.net/private/home-stack-pi5.img.xz \
  --icon-url https://images.example.net/home-stack.png \
  --release-date 2026-10-24 \
  --output dist/os_list.json
```

Serve the catalogue and the exact referenced image; provide your own icon at the specified URL. For manually generated catalogues on a trusted LAN, `--allow-http` permits HTTP image/icon URLs. The local server handles this automatically. Test the catalogue with desktop Imager first:

```sh
rpi-imager --repo https://images.example.net/private/os_list.json
```

The emitted entry uses `devices: ["pi5"]` and `init_format: "none"`. Customization belongs to Combustion, so Imager must not inject Raspberry Pi OS settings. The field meanings come from [Imager's catalogue schema](https://github.com/raspberrypi/rpi-imager/blob/main/doc/json-schema/os-list-schema.json).

## Custom catalogue caveat

EEPROM has an `IMAGER_REPO_URL` setting intended to select the embedded Imager's catalogue. The live Raspberry Pi documentation still carries a warning that this is unsupported because of [issue #894](https://github.com/raspberrypi/rpi-imager/issues/894). However, that issue was closed by [PR #976](https://github.com/raspberrypi/rpi-imager/pull/976), merged on 6 January 2025, specifically fixing the Pi 5 bootloader-config lookup.

These sources conflict. The fix establishes intended support, but this investigation did not boot the currently served installer to verify which build it contains. Check whether your downloaded embedded Imager honours the setting or offers a custom Content Repository option. Do not promise a zero-touch catalogue override from the documentation alone. An example setting for a provisioned EEPROM is:

```ini
IMAGER_REPO_URL=https://images.example.net/private/os_list.json
```

A factory Pi still needs a way to select your catalogue or receive that EEPROM setting. Merely hosting the file cannot cause it to discover your private server.

## Hosting the installer too

For a controlled installation environment, host a compatible installer boot filesystem and set EEPROM `HTTP_HOST` / `HTTP_PATH`; use mode `7` for HTTP boot. A custom host defaults to plain HTTP. Pi 5 supports custom-CA HTTPS with the certificate embedded in EEPROM and `HTTP_CACERT_HASH` configured. A port of 443 alone does not enable TLS. Secure-boot devices need appropriately signed boot files. Follow the [firmware configuration reference](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-bootloader-configuration).

A custom headless installer could download and verify the disk image, identify an allowed NVMe by model/serial, obtain explicit wipe authorization, write/verify it, then reboot. That requires its own installer, signing and destructive-device-selection design. This repository supplies the OS image builder, catalogue generator and local image server; it does not yet build or deploy that installer, change EEPROM settings, or publish images.
