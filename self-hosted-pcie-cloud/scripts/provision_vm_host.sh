#!/usr/bin/env bash
# Run on RayWK with sudo. Does not detach/reset GPU or stop the desktop.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo bash scripts/provision_vm_host.sh'; exit 1; }
apt-get update
apt-get install -y python3-libvirt qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf cloud-image-utils openssh-client dnsmasq-base libguestfs-tools curl ca-certificates
systemctl enable --now libvirtd
# Preserve an existing default network instead of replacing its configuration.
if ! virsh -c qemu:///system net-info default >/dev/null 2>&1; then
  if ip -4 route show table all | grep -Eq '(^| )192\.168\.122\.'; then
    echo '192.168.122.0/24 already routed; choose a non-overlapping NAT subnet before defining default.'
    exit 1
  fi
  network_xml=$(mktemp)
  trap 'rm -f "$network_xml"' EXIT
  cat > "$network_xml" <<'XML'
<network>
  <name>default</name>
  <forward mode="nat"/>
  <bridge name="virbr0" stp="on" delay="0"/>
  <ip address="192.168.122.1" netmask="255.255.255.0">
    <dhcp><range start="192.168.122.2" end="192.168.122.254"/></dhcp>
  </ip>
</network>
XML
  virsh -c qemu:///system net-define "$network_xml"
fi
if ! virsh -c qemu:///system net-list --name | grep -Fxq default; then
  virsh -c qemu:///system net-start default
fi
virsh -c qemu:///system net-autostart default
/usr/bin/python3 -c 'import libvirt; c=libvirt.open("qemu:///system"); print("libvirt connected:",c.getURI()); c.close()'
command -v cloud-localds
virsh -c qemu:///system net-info default
virsh -c qemu:///system net-dumpxml default
seed_dir=$(mktemp -d)
trap 'rm -rf "$seed_dir"; if [[ -n "${network_xml:-}" ]]; then rm -f "$network_xml"; fi' EXIT
printf '#cloud-config\nusers: []\n' > "$seed_dir/user-data"
printf 'instance-id: provisioning-check\n' > "$seed_dir/meta-data"
cloud-localds "$seed_dir/seed.img" "$seed_dir/user-data" "$seed_dir/meta-data"
test -s "$seed_dir/seed.img"
echo 'Host tools/NAT and seed generation verified; guest DHCP/SSH still needs a boot test.'
