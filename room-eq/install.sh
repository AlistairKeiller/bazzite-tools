#!/usr/bin/env bash
# Install system-wide room correction on Bazzite: CamillaDSP (+ its web UI)
# between a virtual PipeWire sink and the audio interface, plus the DRC-FIR
# container that calibrate.py uses to compute the correction. Everything lands
# in your home directory; no rpm-ostree layering.
#
# Usage: ./install.sh [output-node]
#
# Run it as your normal desktop user. output-node is the PipeWire node.name of
# the interface the speaker is on; it defaults to the one Scarlett it finds
# (list them with: pw-cli ls Node | grep alsa_output). Re-running it upgrades
# the binaries and units and keeps your current correction.
#
# The first run restarts PipeWire, which interrupts audio for a moment; if
# AirPlay does not come back by itself: sudo systemctl restart shairport-sync
#
# To remove:
#   systemctl --user disable --now camilladsp camilladsp-link camillagui
#   rm ~/.config/systemd/user/camilla{dsp,dsp-link,gui}.service
#   rm ~/.config/pipewire/pipewire.conf.d/camilladsp-sink.conf
#   rm ~/.local/share/applications/room-eq-calibrate.desktop
#   rm -r ~/camilladsp
#   podman rmi room-eq-drc
#   systemctl --user daemon-reload && systemctl --user restart pipewire
set -euo pipefail

releases=https://github.com/HEnquist
camilladsp_url=$releases/camilladsp/releases/download/v4.1.3/camilladsp-linux-pipewire-amd64.tar.gz
camilladsp_sha256=04388dfbf0999732c51a972fa7c3dc7fa17437333124af7918022445c459851a
camillagui_url=$releases/camillagui-backend/releases/download/v4.1.0/bundle_linux_amd64.tar.gz
camillagui_sha256=86fd3cde575038f312ede7bad0910dc5e46b974cafc048c26115ec3cb9f54792

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dir="$HOME/camilladsp"
config="$dir/configs/room-eq.yml"
output="${1:-}"

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }

# Print the id of the PipeWire node named $1, or nothing.
node_id() {
  pw-dump | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    if ((o.get("info") or {}).get("props") or {}).get("node.name") == sys.argv[1]:
        print(o["id"])
        break' "$1"
}

# Wait up to 15 s for the node named $1 and print its id.
wait_for_node() {
  local id
  for _ in {1..30}; do
    id="$(node_id "$1")"
    [[ -z $id ]] || { echo "$id"; return; }
    sleep 0.5
  done
  return 1
}

[[ $(id -u) -ne 0 ]] || die "run this as your desktop user, not as root or with sudo"
[[ $(uname -m) == x86_64 ]] || die "only x86_64 is supported"
for tool in curl tar sha256sum python3 podman pw-dump pw-link pw-play pw-record wpctl; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> Downloading CamillaDSP and CamillaGUI"
curl -fsSL --parallel -o "$tmp/camilladsp.tar.gz" "$camilladsp_url" -o "$tmp/camillagui.tar.gz" "$camillagui_url"
sha256sum --quiet -c <<EOF || die "a download does not match its expected checksum"
$camilladsp_sha256  $tmp/camilladsp.tar.gz
$camillagui_sha256  $tmp/camillagui.tar.gz
EOF

echo "==> Building the DRC container image"
podman build -q -t room-eq-drc -f "$here/Containerfile" "$here"

echo "==> Installing into $dir"
systemctl --user stop camilladsp.service camillagui.service 2>/dev/null || true
mkdir -p "$dir/configs" "$dir/coeffs"
tar -xzf "$tmp/camilladsp.tar.gz" -C "$dir" camilladsp
rm -rf "$dir/camillagui_backend"
tar -xzf "$tmp/camillagui.tar.gz" -C "$dir"
# The bundle listens on every interface by default; keep the UI local.
gui_conf="$dir/camillagui_backend/_internal/config/camillagui.yml"
sed -i 's/^bind_address:.*/bind_address: "127.0.0.1"/' "$gui_conf"
grep -q '^bind_address: "127.0.0.1"$' "$gui_conf" || die "could not restrict CamillaGUI to localhost in $gui_conf"

echo "==> Creating the \"KH 120A (room EQ)\" sink"
install -D -m 0644 -t "$HOME/.config/pipewire/pipewire.conf.d" "$here/camilladsp-sink.conf"
if [[ -z $(node_id camilladsp-sink) ]]; then
  systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
fi
sink_id="$(wait_for_node camilladsp-sink)" || die "the sink did not appear; see: journalctl --user -u pipewire -e"

if [[ -z $output ]]; then
  # The interface's sink is the Scarlett node that has input ports; give it
  # time to come back after the restart.
  for _ in {1..30}; do
    mapfile -t found < <(pw-link -i | grep -i scarlett | cut -d: -f1 | sort -u)
    [[ ${#found[@]} -eq 0 ]] || break
    sleep 0.5
  done
  [[ ${#found[@]} -eq 1 ]] ||
    die "expected one Scarlett output, found ${#found[@]}; pass the node name: $0 <output-node>"
  output="${found[0]}"
fi
wait_for_node "$output" >/dev/null || die "no PipeWire node named $output"
echo "==> Speaker output: $output"
echo "$output" > "$dir/output-node"

if [[ -f $config ]]; then
  # Configs from before camilladsp-link.service asked WirePlumber to connect things.
  sed -i '/^    autoconnect_to:/d' "$config"
  "$dir/camilladsp" --check "$config" >/dev/null || die "$config is not valid for this CamillaDSP; delete it and re-run"
else
  "$here/calibrate.py" --flat
fi

echo "==> Starting the services"
install -m 0755 "$here/camilladsp-link.sh" "$dir/camilladsp-link.sh"
install -D -m 0644 -t "$HOME/.config/systemd/user" \
  "$here/camilladsp.service" "$here/camilladsp-link.service" "$here/camillagui.service"
systemctl --user daemon-reload
systemctl --user enable camilladsp.service camilladsp-link.service camillagui.service
systemctl --user restart camilladsp.service camilladsp-link.service camillagui.service
wpctl set-default "$sink_id"
# pw-link -l only lists ports that are connected: expect two capture ports and one playback port.
for _ in {1..20}; do
  links="$(pw-link -l | grep -o 'camilladsp-\(capture\|playback\):[^ ]*' | sort -u | wc -l)"
  [[ $links -lt 3 ]] || break
  sleep 0.5
done
if [[ $links -lt 3 ]]; then
  systemctl --user --no-pager status camilladsp.service camilladsp-link.service | tail -n 30 >&2 || true
  pw-link -io >&2 || true
  die "CamillaDSP is not connected to the sink and the interface (ports and logs above)"
fi

echo "==> Adding \"Calibrate room EQ\" to the application menu"
sed "s|@CALIBRATE@|$here/calibrate.py|" "$here/room-eq-calibrate.desktop" > "$tmp/room-eq-calibrate.desktop"
install -D -m 0644 -t "$HOME/.local/share/applications" "$tmp/room-eq-calibrate.desktop"

echo "Done. \"KH 120A (room EQ)\" is now the default output."
echo "Calibrate with: $here/calibrate.py   (or \"Calibrate room EQ\" in the menu)"
echo "Logs: journalctl --user -u camilladsp -f"
