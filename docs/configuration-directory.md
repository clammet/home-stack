# Configuration directories and first-install setup

Keep appliance-specific inputs in a separate private directory. The builder never copies that checkout wholesale: it reads `config.json`, explicitly referenced secret/setup files, and the selected application seed trees. Git metadata, databases and hidden state are not accepted as seed inputs.

```sh
python3 build.py --config-dir examples/configuration --render-only build/example
python3 build-macos.py --config-dir ../my-private-appliance \
  --base-image downloads/qualified-base.raw.xz --base-sha256 '<trusted checksum>'
```

`--config` remains supported for an individual JSON file. It is mutually exclusive with `--config-dir`. Paths in the JSON are relative to the configuration directory, never to the public source checkout. The same resolved inputs are transferred to the temporary Linux VM on macOS; the private checkout is not mounted in the VM.

## Layout

```text
config.json
homeassistant-setup.json
immich/
  immich-config.json
homeassistant/
  configuration.yaml
  automations.yaml
  scripts.yaml
  scenes.yaml
  dashboards/
  home-stack/
    automations.yaml
secrets/
  AGENTS.md
  README.md
  owner-password.secret
  mqtt-password.secret
```

The complete synthetic example is in [`examples/configuration`](../examples/configuration/). Its passwords are fake placeholders and its names describe an invented installation.

## Secret references and seeds

Use `{"secret_file": "secrets/owner-password.secret"}` wherever a JSON string value is needed. The loader removes a trailing line ending, resolves the value internally, and never prints it. References must stay inside `secrets/`; traversal and symlinks are rejected. Do not print the resolved configuration: it intentionally contains the runtime credentials.

An application can specify `"seed": "homeassistant"`. Alternatively:

```json
{
  "seed": {
    "directory": "homeassistant",
    "private_files": {"secrets.yaml": "secrets/homeassistant-location.yaml"}
  },
  "setup": "homeassistant-setup.json"
}
```

Seed files are compiled into the private payload and written **only for the initial application data generation**, before the first container start. Ordinary updates clone the active data and never reapply the original seed. Hidden files/directories, database files, file/directory traversal, nonregular files, oversized files and private-overlay collisions are rejected. A private directory must not be selected wholesale as a seed; copy only explicitly named private files.

The resolved JSON, render-only output, VM input archive and resulting image contain credentials. Output manifests contain image hashes/references, not the resolved configuration or secret values. Use private output locations and the normal build's restrictive permissions.

## Home Assistant initialization

For a `homeassistant` app, `setup` names a version-1 manifest. The controller runs the initializer inside the accepted HA container, using its own Python/aiohttp installation and loopback API. It does not run the initializer against arbitrary network endpoints.

The manifest supports:

- `preferences`: initial frontend user preferences, such as advanced-mode and language display settings, applied once.
- `owner`: new owner's `name`, `username`, `language`, and a private `password` reference. No old authentication database is imported.
- `integrations`: stable `key`, integration `domain`, optional `match_device`, and `steps`, a mapping from HA configuration-flow step IDs to submissions. Optional `options_steps` initializes integration options.
- `floors` and `areas`: each has a stable local `key` and `name`, plus native registry settings such as `icon`, `level`, or `floor_id`.
- `devices`: match an `identifier` or `connection` pair, or a configured integration `entry` key, then set optional `name` and `area`. `required` defaults to true.
- `entities`: match `platform` and hardware-derived `unique_id`, then assign `entity_id`, optional `name`, `icon`, `area` and `disabled_by`. `required` defaults to false. A naming collision is reported rather than reassigning another device's entity.
- `device_settings`: initial `entity_id`, supported `action`, and `data`. Only number values, select options and switch on/off actions are accepted. Successful settings are checkpointed and are not repeatedly enforced over later UI edits.
- `automations`: a relative `source` seed file and a `requires` entity list. Start with an empty `automations.yaml`; activation waits until these entities are available. The initializer copies the staged automations and reloads them once. It refuses to overwrite automations created manually while waiting for pairing.

For radio steps, `{"serial_by_id": "SYNTHETIC-USB-IDENTITY"}` resolves to the container's serial-by-id device path. `{"radio_type_name": "ezsp"}` resolves the HA version's actual radio-choice description. These avoid machine-specific absolute paths and translated radio display strings in the manifest.

Initialization uses the same onboarding/configuration/registry interfaces as HA's frontend. They are not a universal stable provisioning API. Keep HA pinned during commissioning, test a new HA release before changing that pin, and update step mappings when an integration changes its setup flow. Unknown steps stop with a status code; the initializer never guesses a radio reset/migration selection.

## Retry, pairing and user edits

The initializer checkpoints authentication, successful entries, in-progress flows, registry assignments, device settings and automation activation in a private state file inside HA's data generation. This state follows normal application update/rollback copies; it is not exported into the configuration repository.

The normal five-minute reconciliation timer retries unfinished work. To retry immediately after pairing, run `systemctl start home-stack.service`. `controller.py status` includes each application's sanitized `setup` progress, pending items and error codes. Pending pairing is a waiting state; initialization errors are durable faults and use the existing notification system. Runtime HA availability checks remain separate from commissioning completeness.

Already-configured integrations are reused. Existing radio entries are never reset by retries. For a genuinely new radio entry, the submitted network-formation choice is applied, so choose it deliberately. Physical device reset/joining remains a commissioning task; no database, old registry or network-backup migration is performed.

YAML remains the source of truth for YAML-defined settings and dashboards; some of these are read-only in HA's visual editor. API-created settings remain native HA settings. After initial successful application, the initializer preserves later UI changes rather than continually reconciling them back to the original manifest. Changing a seed/manifest for a later deployment is an explicit configuration change, not an automatic migration or a reason to reformat a running appliance.

## Mosquitto preset

The `mosquitto` preset uses `docker.io/eclipse-mosquitto:2`, a single managed configuration/data tree, port 1883, and a TCP availability probe. Set `settings.users` to objects containing `username` and a password secret reference. The first seed generates an authenticated listener with retained-message persistence and converts its private password file to hashes using the bundled image's `mosquitto_passwd`; passwords never appear in command arguments.

TCP availability does not prove authentication/discovery. Commission with a real client, restart both services, and verify retained discovery and reconnect behaviour. This is a trusted-LAN, non-TLS listener by default. If different security/network requirements apply, provide an appropriate dedicated service configuration.

## Immich preset

The synthetic configuration enables Immich with normal onboarding and UI-managed settings. The `immich/immich-config.json` placeholder is inactive until you select `"seed": "immich"` and `"settings": {"config_file": "immich-config.json"}` on that app. File configuration is authoritative and disables editing Immich settings in the UI; it does not create an administrator. See [Immich](immich.md) for configuration options, grouped updates and storage requirements.

## Seafile preset

The synthetic configuration also enables Seafile on port 8080. It initializes its own database and admin account, then presents its normal login page. Omit Seafile settings to generate private initial credentials, or configure `settings.server_url`, `settings.admin_email` and a `settings.admin_password` secret reference. Generic shared-volume seeds are rejected because partial configuration can bypass upstream initialization. See [Seafile first login, settings and grouped updates](seafile.md).

## Validation

Tests cover path confinement, opaque secret resolution, private overlays, database rejection, macOS transfer round trips, first-generation seeding, credential-free command arguments, resumable setup, entity identity/collisions, preservation of UI edits, and delayed automation activation. These are software-level checks. Fresh onboarding on the pinned HA image and Pi hardware acceptance are still required; test success does not claim that a Pi/NVMe image has booted or that devices have paired.

The relevant upstream implementations are [HA onboarding](https://github.com/home-assistant/core/blob/dev/homeassistant/components/onboarding/views.py), [configuration flows](https://github.com/home-assistant/core/blob/dev/homeassistant/components/config/config_entries.py), and the [Mosquitto password tool](https://mosquitto.org/man/mosquitto_passwd-1.html).
