"""Generate the first-boot Combustion seed and systemd services."""
import base64
import json
from pathlib import Path
import shlex
import shutil

RUNTIME = Path(__file__).resolve().parents[1] / "runtime" / "controller.py"


def write(root, name, content, mode=0o600):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def environment(config, app):
    env = dict(app["environment"], TZ=config["timezone"])
    if app["kind"] == "pihole":
        p = app["settings"]
        env.update({
            "FTLCONF_webserver_api_password": p["admin_password"],
            "FTLCONF_webserver_port": "80", "FTLCONF_dns_listeningMode": "ALL",
            "FTLCONF_dns_upstreams": ";".join(p["upstream_dns"]),
            "FTLCONF_dns_dnssec": str(p["dnssec"]).lower(),
            "FTLCONF_dns_domainNeeded": str(p["domain_needed"]).lower(),
            "FTLCONF_dns_cache_optimizer": str(p["cache_optimizer"]),
            "FTLCONF_webserver_session_timeout": str(p["web_session_timeout"]),
            "FTLCONF_dns_hosts": ";".join(["192.0.2.1 home-stack-health.test"] + p["local_records"]),
            "FTLCONF_dns_queryLogging": "false", "FTLCONF_misc_privacylevel": "3",
            "FTLCONF_database_maxDBdays": "0", "FTLCONF_database_DBimport": "false",
            "FTLCONF_database_forceDisk": "false", "FTLCONF_files_database": "/dev/shm/pihole-FTL.db",
            "FTLCONF_webserver_session_restore": "false", "TAIL_FTL_LOG": "0",
            "FTLCONF_ntp_ipv4_active": "false", "FTLCONF_ntp_ipv6_active": "false",
            "FTLCONF_ntp_sync_active": "false",
        })
    return env


def timer(service, calendar, delay):
    return f"""[Unit]
Description=Scheduled {service}
[Timer]
OnCalendar={calendar}
RandomizedDelaySec={delay}
Persistent=true
Unit={service}.service
[Install]
WantedBy=timers.target
"""


def render(config, destination):
    root = Path(destination)
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    payload = root / "payload"
    apps = [a for a in config["apps"] if a["enabled"]]
    n = config["network"]
    network = f"""[connection]
id=home-stack-wired
type=ethernet
autoconnect=true
autoconnect-priority=100
{('interface-name=' + n['interface']) if n['interface'] else ''}
[ipv4]
method={'manual' if n['address'] else 'auto'}
ignore-auto-dns=true
dns={';'.join(n['host_dns'])};
dns-priority=-100
"""
    if n["address"]:
        network += f"address1={n['address']},{n['gateway']}\n"
    network += "[ipv6]\nmethod=disabled\n"
    write(payload, "config.json", json.dumps(config, indent=2) + "\n")
    for app in apps:
        write(payload, app["name"] + ".env", "".join(f"{k}={v}\n" for k, v in environment(config, app).items()))
    shutil.copyfile(RUNTIME, payload / "controller.py")
    shutil.copyfile(RUNTIME.with_name("ha_setup.py"), payload / "ha_setup.py")
    write(payload, "authorized_keys", "\n".join(config["ssh_authorized_keys"]) + "\n")
    write(payload, "journald.conf", f"""[Journal]
Storage={config['logging']['storage']}
MaxLevelStore={config['logging']['max_level']}
RuntimeMaxUse=16M
ForwardToSyslog=no
ForwardToKMsg=no
ForwardToConsole=no
ForwardToWall=no
""", 0o644)
    units = payload / "units"
    for service, action in (("home-stack", "reconcile"), ("home-stack-update", "update"), ("home-stack-os-update", "os-update")):
        write(units, service + ".service", f"""[Unit]
Description=Home Stack {action}
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
Environment=REBOOT_METHOD=systemd
ExecStart=/usr/bin/python3 /etc/home-stack/controller.py {action}
TimeoutStartSec=4h
UMask=0077
[Install]
WantedBy=multi-user.target
""", 0o644)
    write(units, "home-stack.timer", """[Unit]
Description=Recover applications and retry problem notifications
[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
Unit=home-stack.service
[Install]
WantedBy=timers.target
""", 0o644)
    enabled = ["home-stack.service", "home-stack.timer"]
    for key, service in (("container_calendar", "home-stack-update"), ("os_calendar", "home-stack-os-update")):
        if config["updates"][key]:
            write(units, service + ".timer", timer(service, config["updates"][key], config["updates"]["random_delay_seconds"]), 0o644)
            enabled.append(service + ".timer")
    encoded = base64.b64encode(network.encode()).decode()
    script = f"""#!/bin/bash
# combustion: network prepare
set -euo pipefail
umask 077
mkdir -p /etc/NetworkManager/system-connections
printf '%s' '{encoded}' | base64 -d > /etc/NetworkManager/system-connections/home-stack.nmconnection
chmod 600 /etc/NetworkManager/system-connections/home-stack.nmconnection
if [ "${{1-}}" = --prepare ]; then exit 0; fi

zypper --non-interactive install --no-recommends podman python3 health-checker curl bluez
# Persistent subvolumes must be mounted inside the first-boot transaction.
mountpoint -q /var || mount /var
mountpoint -q /usr/local || mount /usr/local
install -d -m 700 /etc/home-stack /var/lib/home-stack
install -m 600 payload/config.json payload/*.env /etc/home-stack/
install -m 644 payload/controller.py payload/ha_setup.py /etc/home-stack/
install -m 644 payload/units/* /etc/systemd/system/
printf '%s\\n' {shlex.quote(config['hostname'])} > /etc/hostname
ln -snf {shlex.quote('/usr/share/zoneinfo/' + config['timezone'])} /etc/localtime

# Keep container archives out of Combustion's RAM copy.
mkdir -p /run/home-stack-bootstrap
mount -o ro,nosuid,nodev,noexec /dev/disk/by-label/combustion /run/home-stack-bootstrap
trap 'umount /run/home-stack-bootstrap' EXIT
"""
    for app in apps:
        name = app["name"]
        script += f"install -d -m 700 /var/lib/home-stack/{name}\ninstall -m 600 /run/home-stack-bootstrap/archives/{name}.tar /var/lib/home-stack/{name}/bootstrap.tar\n"
    script += f"""umount /run/home-stack-bootstrap
trap - EXIT
mkdir -p /etc/systemd/resolved.conf.d /etc/NetworkManager/conf.d
printf '[Resolve]\\nDNSStubListener=no\\n' > /etc/systemd/resolved.conf.d/home-stack.conf
printf '[main]\\ndns=default\\n' > /etc/NetworkManager/conf.d/home-stack.conf
ln -snf /run/NetworkManager/resolv.conf /etc/resolv.conf
systemctl mask transactional-update.timer podman-auto-update.timer kiwi-repart.service
printf 'REBOOT_METHOD=systemd\\nUPDATE_METHOD=dup\\n' > /etc/transactional-update.conf
systemctl enable NetworkManager.service NetworkManager-wait-online.service health-checker.service {' '.join(enabled)}
# The filesystem size is fixed by the builder because the seed is last.
passwd -l root
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPermitRootLogin prohibit-password\\n' > /etc/ssh/sshd_config.d/home-stack.conf
"""
    if any(a["kind"] == "homeassistant" for a in apps):
        script += "systemctl enable bluetooth.service\n"
    if config["ssh_authorized_keys"]:
        script += "install -d -m 700 /root/.ssh\ninstall -m 600 payload/authorized_keys /root/.ssh/authorized_keys\nsystemctl enable sshd.service\n"
    else:
        script += "systemctl disable sshd.service\n"
    script += "if command -v firewall-offline-cmd >/dev/null; then\n"
    for app in apps:
        ports = app["ports"]
        if app["kind"] == "pihole":
            ports = ["53:53/tcp", "53:53/udp", f"{app['settings']['web_port']}:80/tcp"]
        elif app["kind"] == "mosquitto":
            ports = ["1883:1883/tcp"]
        elif app["kind"] == "homeassistant":
            ports = ["8123:8123/tcp", "5353:5353/udp", "1900:1900/udp"]
        elif app["network"] == "host":
            port = app["health"].get("port")
            if app["health"]["kind"] == "http":
                from urllib.parse import urlsplit
                parsed = urlsplit(app["health"]["url"])
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            ports = [f"{port}:{port}/tcp"]
        for port in ports:
            host = port.split(":")[0]
            protocol = port.split("/")[1]
            script += f"    firewall-offline-cmd --add-port={host}/{protocol}\n"
    if config["ssh_authorized_keys"]:
        script += "    firewall-offline-cmd --add-service=ssh\n"
    script += """fi
mkdir -p /etc/systemd/system/health-checker.service.d
cat > /etc/systemd/system/health-checker.service.d/home-stack.conf <<'UNIT'
[Unit]
Wants=home-stack.service
After=home-stack.service
[Service]
TimeoutStartSec=4h
UNIT
# The plugin is a stable shim; versioned code lives on the OS snapshot in /etc.
install -d /usr/local/libexec/health-checker
cat > /usr/local/libexec/health-checker/home-stack.sh <<'PLUGIN'
#!/bin/sh
case "$1" in
  check|stop) exec /usr/bin/python3 /etc/home-stack/controller.py "$1" ;;
esac
PLUGIN
chmod 755 /usr/local/libexec/health-checker/home-stack.sh
mkdir -p /etc/systemd/journald.conf.d /etc/systemd/coredump.conf.d
install -m 644 payload/journald.conf /etc/systemd/journald.conf.d/90-home-stack.conf
systemctl mask rsyslog.service syslog-ng.service syslog.service auditd.service
printf '[Coredump]\\nStorage=none\\nProcessSizeMax=0\\n' > /etc/systemd/coredump.conf.d/90-home-stack.conf
install -m 700 payload/clean-seed.sh /etc/home-stack/clean-seed.sh
systemctl enable home-stack-seed-cleanup.service
"""
    write(units, "home-stack-seed-cleanup.service", """[Unit]
Description=Remove first-boot secrets and archives from seed
After=first-boot-complete.target home-stack.service
[Service]
Type=oneshot
ExecStart=/bin/bash /etc/home-stack/clean-seed.sh
[Install]
WantedBy=multi-user.target
""", 0o644)
    write(payload, "clean-seed.sh", """#!/bin/bash
set -euo pipefail
# Keep seed intact until every application has an accepted generation.
python3 - <<'CHECK'
import json
from pathlib import Path
config = json.loads(Path('/etc/home-stack/config.json').read_text())
for app in config['apps']:
    if app['enabled']:
        state = json.loads((Path('/var/lib/home-stack') / app['name'] / 'state.json').read_text())
        if not state['active'] or state['pending']:
            raise SystemExit(1)
CHECK
mkdir -p /run/home-stack-seed
mount -o rw,nosuid,nodev,noexec /dev/disk/by-label/combustion /run/home-stack-seed
trap 'umount /run/home-stack-seed' EXIT
rm -rf /run/home-stack-seed/combustion /run/home-stack-seed/archives
sync
""", 0o700)
    write(root, "script", script, 0o700)
