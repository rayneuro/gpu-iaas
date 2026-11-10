#!/usr/bin/env bash
set -euo pipefail
echo 'Kernel command line'
cat /proc/cmdline
echo 'IOMMU groups'
find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 -type d | wc -l
echo 'KVM'
ls -l /dev/kvm
echo 'Enable Intel VT-d or AMD IOMMU in firmware. Verify kernel IOMMU support.'
echo 'This diagnostic does not edit GRUB, detach devices or reboot.'
