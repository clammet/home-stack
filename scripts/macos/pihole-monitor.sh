#!/bin/bash
# Per-user launchd DNS monitor. Uses tools supplied with macOS; no Homebrew/Python.
set -euo pipefail

LABEL=local.home-stack.pihole-monitor
MONITOR_DIRECTORY="${HOME_STACK_MONITOR_DIRECTORY:-$HOME/Library/Application Support/Home Stack/Pi-hole Monitor}"
AGENT_DIRECTORY="${HOME_STACK_AGENT_DIRECTORY:-$HOME/Library/LaunchAgents}"
MONITOR_SOURCE_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

launchctl_command() { /bin/launchctl "$@"; }

usage() {
    cat <<'HELP'
Usage:
  pihole-monitor.sh install --server IP [--domain example.com] [--expected IPv4]
                            [--interval 60] [--failures 3]
  pihole-monitor.sh uninstall | status | check | test-notification

Install as your logged-in macOS user, without sudo. Queries the chosen server
using both UDP and TCP. A notification follows N consecutive failed checks,
then one recovery notification. Reinstall to change settings.
HELP
}

fail() { printf '%s\n' "$*" >&2; return 1; }
is_uint() { [[ "$1" =~ ^[0-9]+$ ]] && [[ ${#1} -le 9 ]]; }
is_ipv4() {
    local octet
    local IFS=.
    local -a parts
    read -r -a parts <<< "$1"
    [[ ${#parts[@]} -eq 4 && "$1" != *. ]] || return 1
    for octet in "${parts[@]}"; do
        [[ "$octet" =~ ^[0-9]{1,3}$ ]] && (( 10#$octet <= 255 )) || return 1
    done
}
validate_config() {
    is_ipv4 "$server" || fail 'Server must be a literal IPv4 address.'
    [[ "$domain" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ && ${#domain} -le 253 ]] || fail 'Invalid probe domain.'
    [[ -z "$expected" ]] || is_ipv4 "$expected" || fail 'Expected answer must be IPv4.'
    is_uint "$interval" && (( 10#$interval >= 15 && 10#$interval <= 86400 )) || fail 'Interval must be 15–86400 seconds.'
    is_uint "$threshold" && (( 10#$threshold >= 2 && 10#$threshold <= 100 )) || fail 'Failures must be 2–100.'
    interval=$((10#$interval))
    threshold=$((10#$threshold))
}
read_config() {
    [[ -f "$MONITOR_DIRECTORY/config" ]] || fail 'Monitor is not installed.'
    {
        IFS= read -r server
        IFS= read -r domain
        IFS= read -r expected
        IFS= read -r interval
        IFS= read -r threshold
    } < "$MONITOR_DIRECTORY/config"
    validate_config
}

notify() {
    # Data is passed as argv, never interpolated into AppleScript source.
    /usr/bin/osascript - "$1" <<'APPLESCRIPT'
on run argv
    display notification (item 1 of argv) with title "Pi-hole DNS Monitor"
end run
APPLESCRIPT
}

dns_query() { /usr/bin/dig "$@"; }

probe() {
    local transport answer address
    for transport in +notcp +tcp; do
        if ! answer=$(dns_query "@$server" "$domain" A +short +time=3 +tries=1 "$transport" 2>/dev/null); then
            return 1
        fi
        local found=0
        while IFS= read -r address; do
            if is_ipv4 "$address" && [[ "$address" != 0.0.0.0 ]] && [[ -z "$expected" || "$address" == "$expected" ]]; then
                found=1
            fi
        done <<< "$answer"
        [[ "$found" == 1 ]] || return 1
    done
}

save_state() {
    local temporary
    temporary=$(mktemp "$MONITOR_DIRECTORY/state.XXXXXX")
    printf '%s %s %s\n' "$failures" "$announced" "$now" > "$temporary"
    mv -f "$temporary" "$MONITOR_DIRECTORY/state"
}

# Pure state transition apart from the injected notify function; tested with
# a fake notifier so tests never produce desktop notifications.
record_result() {
    local result=$1
    if (( now < last || now - last > interval * 3 )); then
        failures=0  # Start a fresh consecutive sequence after sleep/logout.
    fi
    if [[ "$result" == healthy ]]; then
        failures=0
        if [[ "$announced" == 1 ]]; then
            if notify "Pi-hole at $server is answering $domain again."; then announced=0; fi
        fi
    else
        if (( failures < threshold )); then failures=$((failures + 1)); fi
        if (( failures >= threshold )) && [[ "$announced" == 0 ]]; then
            if notify "Pi-hole at $server failed $threshold consecutive DNS checks for $domain. The Mac may be offline or away from your LAN."; then announced=1; fi
        fi
    fi
    save_state
}

check() {
    read_config
    umask 077
    local lock="$MONITOR_DIRECTORY/check.lock" pid age
    now=$(date +%s)
    if ! mkdir "$lock" 2>/dev/null; then
        pid=$(cat "$lock/pid" 2>/dev/null || true)
        if is_uint "$pid" && kill -0 "$pid" 2>/dev/null; then return 0; fi
        # Allow a concurrent process time to write its PID before reclaiming.
        age=$(/usr/bin/stat -f %m "$lock" 2>/dev/null || printf '%s' "$now")
        if (( now - age < 30 )); then return 0; fi
        rm -rf "$lock"
        mkdir "$lock" 2>/dev/null || return 0
    fi
    printf '%s\n' "$$" > "$lock/pid"
    trap 'rm -rf "$MONITOR_DIRECTORY/check.lock"' EXIT
    failures=0 announced=0 last=0
    if [[ -f "$MONITOR_DIRECTORY/state" ]]; then
        read -r failures announced last < "$MONITOR_DIRECTORY/state" || true
        if ! is_uint "$failures" || [[ ! "$announced" =~ ^[01]$ ]] || [[ ! "$last" =~ ^[0-9]{1,12}$ ]]; then
            failures=0 announced=0 last=0
        fi
        failures=$((10#$failures))
        last=$((10#$last))
    fi
    if probe; then record_result healthy; else record_result failed; fi
}

install_monitor() {
    [[ "$(uname -s)" == Darwin ]] || fail 'Installation requires macOS.'
    [[ "$(id -u)" != 0 ]] || fail 'Install as the logged-in user, without sudo.'
    local server='' domain=example.com expected='' interval=60 threshold=3
    while (( $# )); do
        [[ $# -ge 2 ]] || fail "Missing value for $1"
        case "$1" in
            --server) server=$2;;
            --domain) domain=$2;;
            --expected) expected=$2;;
            --interval) interval=$2;;
            --failures) threshold=$2;;
            *) fail "Unknown option: $1";;
        esac
        shift 2
    done
    validate_config
    local gui="gui/$(id -u)" destination="$AGENT_DIRECTORY/$LABEL.plist" temporary source
    launchctl_command print "$gui" >/dev/null || fail 'A logged-in graphical user session is required.'
    source=$MONITOR_SOURCE_PATH
    umask 077
    mkdir -p "$MONITOR_DIRECTORY" "$AGENT_DIRECTORY"
    temporary=$(mktemp "$MONITOR_DIRECTORY/agent.XXXXXX")
    # Generate absolute installation paths only on the destination Mac.
    /usr/bin/plutil -create xml1 "$temporary"
    /usr/bin/plutil -insert Label -string "$LABEL" "$temporary"
    /usr/bin/plutil -insert ProgramArguments -json '["/bin/bash"]' "$temporary"
    /usr/bin/plutil -insert ProgramArguments.1 -string "$MONITOR_DIRECTORY/monitor.sh" "$temporary"
    /usr/bin/plutil -insert ProgramArguments.2 -string check "$temporary"
    /usr/bin/plutil -insert StartInterval -integer "$interval" "$temporary"
    /usr/bin/plutil -insert RunAtLoad -bool true "$temporary"
    /usr/bin/plutil -insert ProcessType -string Background "$temporary"
    /usr/bin/plutil -insert LimitLoadToSessionType -string Aqua "$temporary"
    /usr/bin/plutil -lint "$temporary"
    launchctl_command bootout "$gui/$LABEL" 2>/dev/null || true
    if [[ "$source" != "$MONITOR_DIRECTORY/monitor.sh" ]]; then
        cp "$source" "$MONITOR_DIRECTORY/monitor.sh.new"
        chmod 700 "$MONITOR_DIRECTORY/monitor.sh.new"
        mv -f "$MONITOR_DIRECTORY/monitor.sh.new" "$MONITOR_DIRECTORY/monitor.sh"
    fi
    printf '%s\n' "$server" "$domain" "$expected" "$interval" "$threshold" > "$MONITOR_DIRECTORY/config.new"
    mv -f "$MONITOR_DIRECTORY/config.new" "$MONITOR_DIRECTORY/config"
    rm -f "$MONITOR_DIRECTORY/state"
    rm -rf "$MONITOR_DIRECTORY/check.lock"
    chmod 644 "$temporary"
    mv -f "$temporary" "$destination"
    launchctl_command enable "$gui/$LABEL"
    launchctl_command bootstrap "$gui" "$destination"
    printf 'Installed: check %s every %s seconds; notify after %s consecutive failures.\n' "$server" "$interval" "$threshold"
    printf 'Run this script with test-notification, then allow notifications in macOS Settings if needed.\n'
}

uninstall_monitor() {
    [[ "$(uname -s)" == Darwin && "$(id -u)" != 0 ]] || fail 'Uninstall as your macOS user, without sudo.'
    launchctl_command bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT_DIRECTORY/$LABEL.plist"
    # Only delete files this monitor owns, not arbitrary parent directories.
    rm -f "$MONITOR_DIRECTORY/monitor.sh" "$MONITOR_DIRECTORY/config" "$MONITOR_DIRECTORY/state"
    rm -rf "$MONITOR_DIRECTORY/check.lock"
    rmdir "$MONITOR_DIRECTORY" 2>/dev/null || true
    printf 'Pi-hole monitor uninstalled.\n'
}

main() {
    local action=${1:-help}
    if (( $# )); then shift; fi
    case "$action" in
        install) install_monitor "$@";;
        uninstall) uninstall_monitor;;
        check) check;;
        test-notification) notify 'Test notification: Pi-hole DNS monitoring is ready.';;
        status)
            read_config
            printf 'Server: %s\nDomain: %s\nInterval: %ss\nFailure threshold: %s\n' "$server" "$domain" "$interval" "$threshold"
            if [[ -f "$MONITOR_DIRECTORY/state" ]]; then
                read -r failures announced last < "$MONITOR_DIRECTORY/state"
                printf 'Consecutive failures: %s\nOutage announced: %s\nLast check (Unix time): %s\n' "$failures" "$announced" "$last"
            else printf 'No checks recorded yet.\n'; fi
            launchctl_command print "gui/$(id -u)/$LABEL" >/dev/null && printf 'LaunchAgent loaded.\n';;
        help|--help|-h) usage;;
        *) usage >&2; return 1;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
