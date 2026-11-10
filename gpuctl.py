"""Read-only PCI inventory and libvirt domain planner."""
import argparse
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

PCI = Path("/sys/bus/pci/devices")


def inventory():
    devices = {}
    for device in sorted(PCI.iterdir()):
        fields = {key: (device / key).read_text().strip()
                  for key in ("vendor", "device", "class")}
        for key in ("driver", "iommu_group"):
            link = device / key
            fields[key] = link.resolve().name if link.is_symlink() else None
        devices[device.name] = fields
    return devices


def plan(args, devices):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.name):
        raise ValueError("Invalid VM name")
    image = Path(args.image)
    if not image.is_absolute() or args.vcpus < 1 or args.memory < 512:
        raise ValueError("Use an absolute qcow2 path, positive vCPUs and at least 512 MiB")
    allowed = set(json.loads(Path(args.allowlist).read_text())["devices"])
    requested = set(args.pci)
    for address in requested:
        if address not in devices:
            raise ValueError(f"Unknown PCI device: {address}")
        group = devices[address]["iommu_group"]
        if group is None:
            raise ValueError(f"No IOMMU group for {address}")
        members = {bdf for bdf, info in devices.items()
                   if info["iommu_group"] == group}
        if not members <= requested or not members <= allowed:
            raise ValueError(f"IOMMU group {group} requires all members requested "
                             f"and allowlisted: {', '.join(sorted(members))}")
        if devices[address]["class"].startswith("0x06"):
            raise ValueError(f"PCI bridges are excluded: {address}")

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = args.name
    ET.SubElement(domain, "memory", unit="MiB").text = str(args.memory)
    ET.SubElement(domain, "vcpu").text = str(args.vcpus)
    os_node = ET.SubElement(domain, "os", firmware="efi")
    ET.SubElement(os_node, "type", arch="x86_64", machine="q35").text = "hvm"
    hardware = ET.SubElement(domain, "devices")
    disk = ET.SubElement(hardware, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=str(image))
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    nic = ET.SubElement(hardware, "interface", type="network")
    ET.SubElement(nic, "source", network="default")
    ET.SubElement(nic, "model", type="virtio")
    ET.SubElement(hardware, "console", type="pty")
    for bdf in sorted(requested):
        device = ET.SubElement(hardware, "hostdev", mode="subsystem",
                               type="pci", managed="yes")
        segment, bus, slot, function = re.split(r"[:.]", bdf)
        ET.SubElement(ET.SubElement(device, "source"), "address",
                      domain=f"0x{segment}", bus=f"0x{bus}",
                      slot=f"0x{slot}", function=f"0x{function}")
    ET.indent(domain)
    return ET.tostring(domain, encoding="unicode")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    planner = commands.add_parser("plan")
    planner.add_argument("name")
    planner.add_argument("--image", required=True)
    planner.add_argument("--allowlist", default="devices.json")
    planner.add_argument("--pci", nargs="+", required=True)
    planner.add_argument("--memory", type=int, default=8192)
    planner.add_argument("--vcpus", type=int, default=4)
    args = parser.parse_args()
    try:
        devices = inventory()
        if args.command == "inventory":
            print(json.dumps(devices, indent=2))
        else:
            print(plan(args, devices))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
