#!/usr/bin/env bash
# Keep CamillaDSP wired into PipeWire: the "KH 120A (room EQ)" sink feeds its
# two inputs, and its first output feeds the first (left) output of the
# interface named in ~/camilladsp/output-node. Run by camilladsp-link.service.
#
# The links are made here rather than by WirePlumber because WirePlumber cannot
# target a sink's monitor by name, and when a target is missing it falls back
# to the default device, which for the capture side is the microphone.
set -u

output="$(cat "$HOME/camilladsp/output-node")"

# Print the nth (from 0) port whose name starts with $2, from `pw-link -o` or `pw-link -i`.
port() { pw-link "$1" | awk -v prefix="$2" 'index($0, prefix) == 1' | sort | sed -n "$(($3 + 1))p"; }

# Link two ports if both exist; pw-link fails harmlessly when they already are.
link() { [[ -z $1 || -z $2 ]] || pw-link "$1" "$2" 2>/dev/null || true; }

while true; do
  link "$(port -o camilladsp-sink:monitor_ 0)" "$(port -i camilladsp-capture: 0)"
  link "$(port -o camilladsp-sink:monitor_ 1)" "$(port -i camilladsp-capture: 1)"
  link "$(port -o camilladsp-playback: 0)" "$(port -i "$output:" 0)"
  sleep 2
done
