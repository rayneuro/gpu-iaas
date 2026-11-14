#!/usr/bin/env bash
# Build offline; no physical GPU driver changes or guest boot.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID -eq 0 ]] || { echo 'Run this script with sudo'; exit 1; }
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
image_dir=/var/lib/libvirt/images
image_name=ubuntu-22.04-server-cloudimg-amd64.img
image_url=https://cloud-images.ubuntu.com/releases/jammy/release
output=$image_dir/gpu-base.qcow2
driver_package=${DRIVER_PACKAGE:-nvidia-driver-570-server}
[[ "$driver_package" =~ ^nvidia-driver-[0-9]+-server(-open)?$ ]] || exit 1
[[ ! -e "$output" ]] || { echo "Refusing to overwrite $output"; exit 1; }
command -v virt-customize >/dev/null || { echo 'Install libguestfs-tools first'; exit 1; }
install -d -m 0755 "$image_dir"
work_dir=$(mktemp -d "$image_dir/.golden-build.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
curl --fail --location --retry 3 "$image_url/SHA256SUMS" -o "$work_dir/SHA256SUMS"
curl --fail --location --retry 3 "$image_url/$image_name" -o "$work_dir/$image_name"
(cd "$work_dir"; awk -v name="$image_name" '$2 == name || $2 == "*"name' SHA256SUMS > selected.sha256; test -s selected.sha256; sha256sum -c selected.sha256)
qemu-img convert -f qcow2 -O qcow2 "$work_dir/$image_name" "$work_dir/golden.qcow2"
qemu-img resize "$work_dir/golden.qcow2" 32G
export LIBGUESTFS_BACKEND=direct
virt-customize --format qcow2 -a "$work_dir/golden.qcow2" --memsize 4096   --run-command 'growpart /dev/sda 1 && resize2fs /dev/sda1'   --run-command 'apt-get update'   --install "cloud-init,openssh-server,python3,linux-generic,linux-headers-generic,$driver_package"   --upload "$project_dir/scripts/gpu_compute_check.py:/usr/local/bin/gpu-compute-check.py"   --run-command 'systemctl enable ssh; update-initramfs -u -k all; command -v nvidia-smi; test -n "$(find /lib/modules -name nvidia.ko\* -print -quit)"'   --run-command 'cloud-init clean --logs; rm -f /etc/ssh/ssh_host_*; truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id'
qemu-img check "$work_dir/golden.qcow2"
# Publish without replacing any pre-existing golden image.
chmod 0444 "$work_dir/golden.qcow2"
ln "$work_dir/golden.qcow2" "$output"
cp "$work_dir/selected.sha256" "$output.source.sha256"
sha256sum "$output" > "$output.sha256"
echo "Built $output. Driver boot, SSH and GPU compute acceptance are still pending."
