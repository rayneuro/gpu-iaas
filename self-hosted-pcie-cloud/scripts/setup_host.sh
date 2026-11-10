#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "--install" ]]; then
  echo 'Review and run with --install to install Ubuntu build and VM dependencies.'
  echo 'g++ cmake python3 python3-libvirt qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf pciutils cloud-image-utils openssh-client'
  exit 0
fi
sudo apt-get update
sudo apt-get install -y g++ cmake python3 python3-libvirt qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf pciutils cloud-image-utils openssh-client
