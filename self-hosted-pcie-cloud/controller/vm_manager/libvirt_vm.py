import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET


def run(arguments, timeout=60):
    return subprocess.run(arguments, check=True, text=True, capture_output=True, timeout=timeout).stdout


def domain_xml(identifier, spec, image, seed):
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = "gpu-" + identifier
    ET.SubElement(domain, "uuid").text = identifier
    ET.SubElement(domain, "memory", unit="MiB").text = str(spec["memory_mib"])
    ET.SubElement(domain, "vcpu").text = str(spec["vcpus"])
    os_node = ET.SubElement(domain, "os", firmware="efi")
    ET.SubElement(os_node, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(domain, "cpu", mode="host-passthrough")
    ET.SubElement(domain, "on_crash").text = "destroy"
    hardware = ET.SubElement(domain, "devices")
    disk = ET.SubElement(hardware, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=str(image))
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    cd = ET.SubElement(hardware, "disk", type="file", device="cdrom")
    ET.SubElement(cd, "driver", name="qemu", type="raw")
    ET.SubElement(cd, "source", file=str(seed))
    ET.SubElement(cd, "target", dev="sda", bus="sata")
    ET.SubElement(cd, "readonly")
    nic = ET.SubElement(hardware, "interface", type="network")
    ET.SubElement(nic, "mac", address=spec["mac"])
    ET.SubElement(nic, "source", network=spec["network"])
    ET.SubElement(nic, "model", type="virtio")
    ET.SubElement(hardware, "console", type="pty")
    for bdf in spec["devices"]:
        segment, bus, slot_func = bdf.split(":")
        slot, function = slot_func.split(".")
        host = ET.SubElement(hardware, "hostdev", mode="subsystem", type="pci", managed="no")
        ET.SubElement(ET.SubElement(host, "source"), "address",
                      domain="0x" + segment, bus="0x" + bus, slot="0x" + slot,
                      function="0x" + function)
    ET.indent(domain)
    return ET.tostring(domain, encoding="unicode")


class LibvirtVM:
    def __init__(self, config):
        import libvirt
        self.libvirt = libvirt
        self.connection = libvirt.open(config["libvirt_uri"])
        self.config = config

    def domain(self, identifier):
        try:
            return self.connection.lookupByUUIDString(identifier)
        except self.libvirt.libvirtError as error:
            if error.get_error_code() == self.libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise

    def assert_unused(self, devices, owner):
        for domain in self.connection.listAllDomains():
            if domain.UUIDString() == owner:
                continue
            tree = ET.fromstring(domain.XMLDesc(0))
            for address in tree.findall("./devices/hostdev/source/address"):
                bdf = "%04x:%02x:%02x.%x" % tuple(
                    int(address.attrib[key], 16) for key in ("domain", "bus", "slot", "function"))
                if bdf in devices:
                    raise RuntimeError("Device referenced by another libvirt domain")

    def boot(self, identifier, spec):
        directory = Path(self.config["images_dir"]) / identifier
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / "root.qcow2"
        seed = directory / "seed.iso"
        if not image.exists():
            run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", spec["image"], str(image)])
        # Generate unique guest keys through a trusted host provisioning path.
        # Never learn host keys from an unauthenticated network scan.
        hostkey = directory / "ssh_host_ed25519_key"
        if not hostkey.exists():
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)])
        userdata = {"ssh_keys": {"ed25519_private": hostkey.read_text(),
                                  "ed25519_public": hostkey.with_suffix(".pub").read_text()},"users": [{"name": "gpu", "shell": "/bin/bash",
                              "ssh_authorized_keys": [spec["ssh_public_key"]]}],
                    "ssh_pwauth": False}
        for name in ("user-data", "meta-data", "seed.iso"):
            target = directory / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            target.chmod(0o600)
        (directory / "user-data").write_text("#cloud-config\n" + json.dumps(userdata))
        (directory / "meta-data").write_text(json.dumps({"instance-id": identifier,
                                                        "local-hostname": "gpu-" + identifier[:8]}))
        run(["cloud-localds", str(seed), str(directory / "user-data"), str(directory / "meta-data")])
        (directory / "known_hosts").write_text(identifier + " " + hostkey.with_suffix(".pub").read_text())
        xml = domain_xml(identifier, spec, image, seed)
        (directory / "domain.xml").write_text(xml)
        domain = self.connection.defineXML(xml)
        domain.setAutostart(0)
        domain.create()

    def verify(self, identifier, spec):
        deadline = time.monotonic() + self.config["boot_timeout"]
        last_error = "No DHCP lease"
        while time.monotonic() < deadline:
            domain = self.domain(identifier)
            if domain is None or not domain.isActive():
                raise RuntimeError("VM stopped before GPU verification")
            network = self.connection.networkLookupByName(spec["network"])
            ips = [lease["ipaddr"] for lease in network.DHCPLeases()
                   if lease.get("mac", "").lower() == spec["mac"] and lease.get("type") == 0]
            if ips:
                # A dedicated known_hosts file is required. Host keys must be
                # provisioned by the administrator or via a trusted image path.
                command = ["ssh", "-i", self.config["ssh_private_key"], "-o", "BatchMode=yes",
                           "-o", "StrictHostKeyChecking=yes", "-o",
                           "UserKnownHostsFile=" + str(Path(self.config["images_dir"]) / identifier / "known_hosts"),
                           "-o", "HostKeyAlias=" + identifier,
                           "-o", "ConnectTimeout=5", "gpu@" + ips[0],
                           "nvidia-smi --query-gpu=uuid,name,driver_version --format=csv,noheader"]
                try:
                    output = run(command, timeout=15)
                    if "GPU-" not in output:
                        raise RuntimeError("Guest reported no GPU UUID")
                    compute = run(command[:-1] + ["python3 /usr/local/bin/gpu-compute-check.py"], timeout=30)
                    compute = json.loads(compute)
                    if compute.get("cuda_compute") != "PASS":
                        raise RuntimeError("Guest CUDA computation did not pass")
                    return {"ip": ips[0], "nvidia_smi": output.strip(), "compute": compute}
                except (subprocess.SubprocessError, RuntimeError) as error:
                    last_error = str(error)
            time.sleep(2)
        raise TimeoutError("Guest verification timeout: " + last_error)

    def stop(self, identifier, force=False):
        domain = self.domain(identifier)
        if domain is None:
            return
        if domain.isActive():
            if force:
                domain.destroy()
            else:
                domain.shutdown()
                deadline = time.monotonic() + self.config["shutdown_timeout"]
                while domain.isActive() and time.monotonic() < deadline:
                    time.sleep(1)
                if domain.isActive():
                    raise TimeoutError("Guest shutdown timed out. Retry with --force.")
        if domain.isActive():
            raise RuntimeError("Domain is still active")
        # Remove stale PCI ownership references before allowing resource release.
        domain.undefineFlags(self.libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
