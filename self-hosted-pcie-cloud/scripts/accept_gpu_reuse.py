"""Root-only A -> stop -> B -> stop acceptance. Interrupts the desktop."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.main import dispatch
from controller.api.device_client import DeviceClient


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maintenance", action="store_true", required=True,
                        help="Allow interruption of display-manager and teamviewerd")
    parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo /usr/bin/python3")
    if subprocess.run(["pgrep", "-x", "pcie-device-dae"], stdout=subprocess.DEVNULL).returncode == 0:
        raise SystemExit("Another device daemon may own the pool; inspect before acceptance")
    run(["modprobe", "vfio-pci"])
    os.umask(0o077)
    state = Path("/var/lib/gpu-iaas-acceptance")
    state.mkdir(exist_ok=True)
    # Refuse to overwrite recovery records or silently discard an old run.
    config_file = state / "controller.json"
    if config_file.exists():
        raise SystemExit("Existing acceptance state: inspect/recover it before another run")
    image = Path("/var/lib/libvirt/images/gpu-base.qcow2")
    if not image.is_file():
        raise SystemExit("Build golden image first")
    import libvirt
    connection = libvirt.open("qemu:///system")
    network = connection.networkLookupByName("default")
    if not network.isActive():
        raise SystemExit("default network is inactive")
    connection.close()
    bdfs = ["0000:01:00.0", "0000:01:00.1"]
    def drivers():
        return {bdf: (Path("/sys/bus/pci/devices") / bdf / "driver").resolve().name for bdf in bdfs}
    original = drivers()
    if original != {bdfs[0]: "nvidia", bdfs[1]: "snd_hda_intel"}:
        raise SystemExit("Unexpected original drivers; investigate first")
    key = state / "client_key"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    allowlist = state / "devices.allowlist"
    allowlist.write_text("\n".join(bdfs) + "\n")
    devices_state = state / "devices"
    devices_state.mkdir()
    (devices_state / "reset-bridges.allowlist").write_text("0000:00:01.0\n")
    socket = state / "device.sock"
    config = json.loads((ROOT / "configs/controller.json").read_text())
    config.update(execute=True, device_socket=str(socket), database=str(state / "controller.sqlite"),
                  images_dir=str(state / "instances"), allowed_images=[str(image)],
                  ssh_private_key=str(key), boot_timeout=600)
    config_file.write_text(json.dumps(config, indent=2))
    active_services = [name for name in ["display-manager", "teamviewerd"]
                       if subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0]
    daemon = None
    safe_restore = True
    evidence = []
    log = (state / "daemon.log").open("a")
    try:
        for name in active_services:
            run(["systemctl", "stop", name])
        # Daemon itself verifies idle handles, complete IOMMU membership and reset scope.
        daemon = subprocess.Popen([str(ROOT / "build/pcie-device-daemon"), str(socket),
                                   str(devices_state), str(allowlist), "/sys", "--execute"],
                                  stdout=log, stderr=log)
        client = DeviceClient(str(socket))
        for _ in range(100):
            if daemon.poll() is not None:
                raise RuntimeError("Device daemon exited; inspect daemon.log")
            try:
                client.request("STATUS")
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.1)
        else:
            raise TimeoutError("Device daemon startup timeout")
        spec = dict(gpu=bdfs[0], image=str(image), memory_mib=8192, vcpus=4,
                    ssh_public_key=key.with_suffix(".pub").read_text().strip(), network="default")
        safe_restore = False
        for label in ("A", "B"):
            instance = dispatch(str(config_file), "create", "acceptance-" + label, spec)
            if instance["state"] != "READY":
                raise RuntimeError("Guest did not reach READY")
            evidence.append({"guest": label, "id": instance["id"], "evidence": instance["error"]})
            (state / "evidence.json").write_text(json.dumps(evidence, indent=2))
            dispatch(str(config_file), "stop", identifier=instance["id"])
            if client.request("STATUS")["leases"] or drivers() != original:
                raise RuntimeError("Release did not restore original drivers and clear leases")
        safe_restore = True
        run(["nvidia-smi"])
        print("PASS: A CUDA -> release -> B CUDA -> release; evidence:", state / "evidence.json")
    finally:
        if daemon and daemon.poll() is None:
            try:
                status = DeviceClient(str(socket)).request("STATUS")
                safe_restore = not status["leases"] and drivers() == original
            except Exception:
                safe_restore = False
            if safe_restore:
                daemon.terminate()
                daemon.wait(timeout=10)
        if safe_restore:
            for name in reversed(active_services):
                run(["systemctl", "start", name])
        else:
            print("CLEANUP REQUIRED: desktop remains stopped; daemon retained if alive.", file=sys.stderr)
            print("Inspect", config_file, "and daemon.log. Retry controller stop; do not delete journals.", file=sys.stderr)
        log.close()

if __name__ == "__main__":
    main()
