#!/usr/bin/env bash
# Install Shairport Sync (AirPlay 2) on Bazzite as a rootful Podman Quadlet
# that plays through your desktop's PipeWire session.
#
# Usage: ./install.sh [airplay-name]
#
# Run it as your normal desktop user (it calls sudo itself) so it can find your
# PipeWire socket. The name defaults to "Bazzite". Re-running it reinstalls the
# unit and restarts the service.
#
# To remove:
#   sudo systemctl stop shairport-sync
#   sudo rm /etc/containers/systemd/shairport-sync.container
#   sudo systemctl daemon-reload
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/shairport-sync.container"
dest=/etc/containers/systemd/shairport-sync.container
image=docker.io/mikebrady/shairport-sync:latest
name="${1:-Bazzite}"
uid="$(id -u)"

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }

[[ $uid -ne 0 ]] || die "run this as your desktop user, not as root or with sudo"
[[ -f $src ]] || die "$src not found"
command -v podman >/dev/null || die "podman not found"
# Limit the name to characters that need no escaping in sed or a systemd Exec= line.
name_re="^[[:alnum:] ._'-]+$"
[[ $name =~ $name_re ]] || die "the AirPlay name may only contain letters, digits, spaces and . _ ' -"

if [[ ! -S /run/user/$uid/pipewire-0 ]]; then
  warn "/run/user/$uid/pipewire-0 is missing; the service will keep retrying until PipeWire is running in your session"
fi
if systemctl --user is-active --quiet shairport-sync.service 2>/dev/null; then
  warn "a user-level shairport-sync.service is also running; turn it off with: systemctl --user disable --now shairport-sync"
fi

echo "==> Making sure avahi-daemon is running"
systemctl is-active --quiet avahi-daemon.service || sudo systemctl enable --now avahi-daemon.service
# The container's startup script waits for this file before launching Shairport Sync.
[[ -f /run/avahi-daemon/pid ]] || warn "/run/avahi-daemon/pid not found; the container will wait until it exists"

echo "==> Pulling $image"
sudo podman pull "$image"

echo "==> Installing $dest (UID $uid, name \"$name\")"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed -e "s|/run/user/1000/|/run/user/$uid/|" \
    -e "s|^\(Exec=.* -a \).*|\1\"$name\"|" \
    "$src" > "$tmp"
sudo install -D -m 0644 "$tmp" "$dest"

if command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
  echo "==> Opening firewall ports"
  # AirPlay 2 port list from Shairport Sync's TROUBLESHOOTING.md.
  sudo firewall-cmd --permanent --add-service=mdns
  sudo firewall-cmd --permanent \
    --add-port=319-320/udp --add-port=3689/tcp --add-port=5000/tcp --add-port=7000/tcp \
    --add-port=6000-6009/udp --add-port=32768-60999/tcp --add-port=32768-60999/udp
  sudo firewall-cmd --reload
fi

echo "==> Starting shairport-sync.service"
sudo systemctl daemon-reload
systemctl cat shairport-sync.service >/dev/null 2>&1 ||
  die "Quadlet did not generate shairport-sync.service; see: sudo /usr/libexec/podman/quadlet -dryrun"
sudo systemctl restart shairport-sync.service ||
  die "the service failed to start; see: journalctl -u shairport-sync -e"

echo "Done. \"$name\" should now show up under AirPlay on your iPhone."
echo "Logs: journalctl -u shairport-sync -f"
