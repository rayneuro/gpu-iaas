"""Integration checks using a synthetic sysfs tree and the real C++ daemon."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from controller.api.device_client import DeviceClient
from controller.models.store import Store
from controller.scheduler.manager import Manager
from controller.vm_manager.libvirt_vm import domain_xml

ROOT = Path(__file__).resolve().parents[1]


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pcie-test-")
        self.root = Path(self.temporary.name)
        self.sysfs = self.root / "sys"
        self.bdfs = ["0000:01:00.0", "0000:01:00.1"]
        self.group = self.sysfs / "kernel/iommu_groups/16"
        (self.group / "devices").mkdir(parents=True)
        for bdf, device_class in zip(self.bdfs, ["0x030000", "0x040300"]):
            device = self.sysfs / "bus/pci/devices" / bdf
            device.mkdir(parents=True)
            for field, value in {"vendor": "0x10de", "device": "0x2684", "class": device_class}.items():
                (device / field).write_text(value + "\n")
            (device / "iommu_group").symlink_to(self.group)
            (self.group / "devices" / bdf).symlink_to(device)
        self.allowlist = self.root / "allowlist"
        self.allowlist.write_text("\n".join(self.bdfs))
        self.socket_path = self.root / "device.sock"
        self.start_daemon()
        self.client = DeviceClient(str(self.socket_path))
        self.store = Store(self.root / "db.sqlite")
        self.manager = Manager({"execute": False, "cpu_capacity": 8,
                                "memory_capacity_mib": 16384, "allowed_networks": ["default"]},
                               self.store, self.client)
        self.spec = {"gpu": self.bdfs[0], "image": "/base.qcow2", "memory_mib": 8192,
                     "vcpus": 4, "ssh_public_key": "ssh-ed25519 test", "network": "default"}

    def start_daemon(self):
        self.daemon = subprocess.Popen(
            [str(ROOT / "build/pcie-device-daemon"), str(self.socket_path),
             str(self.root / "state"), str(self.allowlist), str(self.sysfs)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.daemon.poll() is not None:
                raise RuntimeError("Daemon exited")
            if self.socket_path.exists():
                try:
                    DeviceClient(str(self.socket_path)).request("STATUS")
                    return
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
            time.sleep(0.02)
        raise TimeoutError("Daemon did not start")

    def tearDown(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.store.connection.close()
        self.temporary.cleanup()

    def test_create_stop_reallocate_and_idempotency(self):
        first = self.manager.create("request-1", self.spec)
        self.assertEqual(first["state"], "SIMULATED_READY")
        self.assertEqual(self.manager.create("request-1", self.spec)["id"], first["id"])
        with self.assertRaisesRegex(RuntimeError, "reserved"):
            self.manager.create("request-2", self.spec)
        self.manager.stop(first["id"])
        second = self.manager.create("request-3", self.spec)
        self.assertEqual(second["state"], "SIMULATED_READY")

    def test_reservation_survives_restart(self):
        self.client.request("ACQUIRE", "owner-a", self.bdfs[0])
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.start_daemon()
        with self.assertRaisesRegex(RuntimeError, "reserved"):
            self.client.request("ACQUIRE", "owner-b", self.bdfs[0])
        self.client.request("RELEASE", "owner-a")
        self.client.request("ACQUIRE", "owner-b", self.bdfs[0])

    def test_entire_group_must_be_allowlisted(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.socket_path.unlink()
        self.allowlist.write_text(self.bdfs[0])
        self.start_daemon()
        with self.assertRaisesRegex(RuntimeError, "not allowlisted"):
            self.client.request("ACQUIRE", "owner", self.bdfs[0])
        self.assertEqual(self.client.request("STATUS")["leases"], [])

    def test_changed_request_is_rejected(self):
        self.manager.create("same", self.spec)
        with self.assertRaisesRegex(ValueError, "different specification"):
            self.manager.create("same", dict(self.spec, vcpus=2))

    def test_group_serialization_and_owner_release(self):
        from concurrent.futures import ThreadPoolExecutor
        def reserve(owner):
            try:
                self.client.request("ACQUIRE", owner, self.bdfs[0])
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(reserve, ["a", "b"])), 1)
        self.client.request("RELEASE", "unrelated")
        self.assertEqual(len(self.client.request("STATUS")["leases"]), 1)

    def test_guest_failure_holds_lease_until_cleanup_succeeds(self):
        class FailingVM:
            def assert_unused(self, *_):
                pass

            def boot(self, *_):
                pass

            def verify(self, *_):
                raise RuntimeError("Guest NVIDIA driver failed")

            def stop(self, *_):
                raise TimeoutError("Guest is still active")

        self.manager.vm = FailingVM()
        with self.assertRaisesRegex(RuntimeError, "NVIDIA"):
            self.manager.create("guest-failure", self.spec)
        instance = self.store.list()[0]
        self.assertEqual(instance["state"], "CLEANUP_REQUIRED")
        with self.assertRaisesRegex(TimeoutError, "still active"):
            self.manager.stop(instance["id"])
        self.assertEqual(len(self.client.request("STATUS")["leases"]), 1)
        self.manager.vm = None
        self.manager.stop(instance["id"])
        self.assertEqual(self.client.request("STATUS")["leases"], [])

    def test_reconcile_reports_orphan_without_freeing_it(self):
        self.client.request("ACQUIRE", "orphan-owner", self.bdfs[0])
        result = self.manager.reconcile()
        self.assertEqual(result["orphan_leases"][0]["owner"], "orphan-owner")
        self.assertEqual(len(self.client.request("STATUS")["leases"]), 1)

    def test_journal_write_failure_blocks_further_mutations(self):
        blocked = self.root / "state/leases.tmp"
        blocked.mkdir()
        with self.assertRaisesRegex(RuntimeError, "journal"):
            self.client.request("ACQUIRE", "first-owner", self.bdfs[0])
        blocked.rmdir()
        with self.assertRaisesRegex(RuntimeError, "Journal failure"):
            self.client.request("ACQUIRE", "second-owner", self.bdfs[0])

    def test_xml_escapes_paths_and_uses_manual_hostdev(self):
        import xml.etree.ElementTree as ET
        spec = dict(self.spec, devices=self.bdfs, mac="52:54:00:01:02:03")
        tree = ET.fromstring(domain_xml("test", spec, "/image&a.qcow2", "/seed.iso"))
        self.assertEqual(len(tree.findall("./devices/hostdev")), 2)
        self.assertTrue(all(node.get("managed") == "no" for node in tree.findall("./devices/hostdev")))
        self.assertEqual(tree.find("./devices/disk/source").get("file"), "/image&a.qcow2")


if __name__ == "__main__":
    unittest.main()
