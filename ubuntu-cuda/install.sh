#!/usr/bin/env bash
# Create an Ubuntu 24.04 distrobox on Bazzite with its own home directory, the
# host's NVIDIA driver and uv, check that PyTorch runs on the GPU in it, and
# turn on Tailscale SSH so you can get into it from your other machines.
#
# Usage: ./install.sh [box-name]
#
# Run it as your normal desktop user on an NVIDIA Bazzite image, with Tailscale
# up. The box is called "cuda" and its home is ~/distrobox/<box-name>.
# Re-running it keeps the box and its files, and updates uv.
#
# To remove:
#   distrobox rm -f cuda
#   rm -rf ~/distrobox/cuda
#   sudo tailscale set --ssh=false   (if nothing else uses Tailscale SSH)
set -euo pipefail

image=quay.io/toolbx/ubuntu-toolbox:24.04
name="${1:-cuda}"
box_home="$HOME/distrobox/$name"

die() { echo "error: $*" >&2; exit 1; }

[[ $(id -u) -ne 0 ]] || die "run this as your desktop user, not as root or with sudo"
for tool in curl distrobox podman nvidia-smi tailscale python3; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
nvidia-smi -L >/dev/null || die "nvidia-smi does not work; this needs an NVIDIA Bazzite image with the driver loaded"
tailscale ip -4 >/dev/null || die "Tailscale is not up; start it with: sudo tailscale up"

if podman container exists "$name"; then
  echo "==> Using the existing $name box"
else
  echo "==> Creating the $name box ($image, home $box_home)"
  mkdir -p "$box_home"
  # --nvidia makes the box use the host's driver, so it never installs one of
  # its own. torch.compile needs a C compiler and the Python headers.
  distrobox create --yes --nvidia --name "$name" --image "$image" --home "$box_home" \
    --additional-packages "build-essential curl python3-dev"
fi

echo "==> Installing uv in $name"
curl -LsSf https://astral.sh/uv/install.sh | distrobox enter "$name" -- sh

echo "==> Checking that PyTorch runs on the GPU (this also caches it for uv)"
distrobox enter "$name" -- "$box_home/.local/bin/uv" run --no-project --with torch python - <<'EOF'
import torch

assert torch.cuda.is_available(), "PyTorch cannot see a CUDA GPU"
x = torch.randn(4096, 4096, device="cuda")
(x @ x).sum().item()
print(f"PyTorch {torch.__version__} (CUDA {torch.version.cuda}) ran on {torch.cuda.get_device_name()}")
EOF

echo "==> Turning on Tailscale SSH"
sudo tailscale set --ssh
host="$(tailscale status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

echo "Done. From another machine on your tailnet: ssh -t $USER@$host distrobox enter $name"
echo "Or add this to ~/.ssh/config there and use: ssh $name"
echo
echo "Host $name"
echo "  HostName $host"
echo "  User $USER"
echo "  RequestTTY yes"
echo "  RemoteCommand distrobox enter $name"
