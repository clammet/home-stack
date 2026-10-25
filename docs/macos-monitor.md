# macOS Pi-hole DNS monitor

`scripts/macos/pihole-monitor.sh` installs a per-user LaunchAgent using macOS's built-in Bash, `dig`, `launchctl`, `plutil` and AppleScript. It does not need Homebrew or Python. Run installation as your logged-in user, without sudo:

```sh
bash scripts/macos/pihole-monitor.sh install --server 192.168.1.2
bash scripts/macos/pihole-monitor.sh test-notification
```

Replace the example IP with the Pi's reserved/static IPv4 address. The default is one check every 60 seconds and an alert after three consecutive failures. Each check sends A-record queries directly to that Pi-hole over UDP and TCP, independently of the Mac's selected DNS server. A healthy result requires a valid nonzero IPv4 answer from both transports. Timeouts, DNS errors with no answers, and missing/mismatched answers count as failures.

The default domain, `example.com`, checks ordinary resolution, which may be answered from cache. It cannot prove that the upstream resolver is reachable on every check. To check this appliance's local DNS canary instead:

```sh
bash scripts/macos/pihole-monitor.sh install \
  --server 192.168.1.2 \
  --domain home-stack-health.test --expected 192.0.2.1 \
  --interval 60 --failures 3
```

The canary tests local Pi-hole availability without depending on internet DNS. You can choose another domain you know will not be blocked.

There is one outage notification at the threshold and one recovery notification. Healthy checks reset the count. A gap exceeding three intervals, such as sleep/logout, starts a new consecutive-failure sequence. A failed notification command is retried. Notifications can still be suppressed by macOS permissions or Focus; run `test-notification` and allow its sender in System Settings → Notifications. Apple describes how scripted notifications register their sender in [Displaying Notifications](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/DisplayNotifications.html).

This runs while the user is logged in, not while the Mac is asleep. An unreachable Pi while you are away from the home LAN also produces an outage notice; the message identifies that possibility. No recurring alert spam is produced for the same outage.

## New-Mac setup integration

The supplied `tmp/setupstuff.sh` now includes an opt-in installation block. Run your existing setup with:

```sh
PIHOLE_SERVER=192.168.1.2 bash tmp/setupstuff.sh
```

It also accepts `PIHOLE_PROBE_DOMAIN`, `PIHOLE_CHECK_INTERVAL` and `PIHOLE_FAILURE_THRESHOLD`. If you copy that setup script outside this checkout, set `HOME_STACK_CHECKOUT` to the checkout location as well. When `PIHOLE_SERVER` is absent, the setup script skips monitoring rather than guessing a DNS server.

The reusable call from any setup script is:

```sh
bash "$HOME_STACK_CHECKOUT/scripts/macos/pihole-monitor.sh" install \
  --server "$PIHOLE_SERVER" --interval 60 --failures 3
```

Installation copies the monitor into the user's Application Support folder and generates its LaunchAgent with paths for that Mac. The checkout can subsequently move or be removed. Reinstalling replaces its configuration and resets the failure counter. The installer validates the generated plist and registers it in the current GUI login session, following Apple's [LaunchAgent model](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

```sh
bash scripts/macos/pihole-monitor.sh status
bash scripts/macos/pihole-monitor.sh check
bash scripts/macos/pihole-monitor.sh uninstall
```

`status` reports the server, failure counter, last-check time and whether the LaunchAgent is loaded. No DNS query history or continuous output log is retained, only the small current-state file. Uninstall removes this monitor's files and job. The downloaded hardware page and the personal setup script live in ignored `tmp/`; they are not project distribution files.
