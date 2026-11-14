import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from controller.vm_manager.libvirt_vm import LibvirtVM

class GuestProvisioningTest(unittest.TestCase):
    def test_unique_host_key_injected_before_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            vm = object.__new__(LibvirtVM)
            vm.config = {"images_dir": tmp}
            class Domain:
                def setAutostart(self, flag): pass
                def create(self): pass
            class Connection:
                def defineXML(self, xml): return Domain()
            vm.connection = Connection()
            spec = dict(image="/base.qcow2", ssh_public_key="ssh-ed25519 user",
                        memory_mib=1024, vcpus=1, network="default", mac="52:54:00:00:00:01", devices=[])
            from controller.vm_manager.libvirt_vm import run
            def fake_run(args, **kwargs):
                if args[0] == "ssh-keygen": return run(args, **kwargs)
                return ""
            with patch("controller.vm_manager.libvirt_vm.run", side_effect=fake_run):
                vm.boot("guest-a", spec)
                vm.boot("guest-b", spec)
            a, b = Path(tmp)/"guest-a", Path(tmp)/"guest-b"
            userdata = json.loads((a/"user-data").read_text().split("\n", 1)[1])
            self.assertEqual(userdata["ssh_keys"]["ed25519_private"], (a/"ssh_host_ed25519_key").read_text())
            self.assertNotEqual((a/"ssh_host_ed25519_key.pub").read_text(), (b/"ssh_host_ed25519_key.pub").read_text())
            self.assertTrue((a/"known_hosts").read_text().startswith("guest-a ssh-ed25519 "))
            self.assertEqual((a/"user-data").stat().st_mode & 0o777, 0o600)

    def test_guest_verification_requires_compute_result(self):
        vm = object.__new__(LibvirtVM)
        vm.config = dict(images_dir="/images", ssh_private_key="/key", boot_timeout=0.1)
        class Domain:
            def isActive(self): return True
        class Network:
            def DHCPLeases(self): return [{"ipaddr":"192.168.122.2", "mac":"test", "type":0}]
        class Connection:
            def networkLookupByName(self, name): return Network()
        vm.connection = Connection()
        vm.domain = lambda identifier: Domain()
        with patch("controller.vm_manager.libvirt_vm.run", side_effect=["GPU-test, RTX, 570", '{"cuda_compute":"PASS"}']) as mock:
            result = vm.verify("guest-a", {"network":"default", "mac":"test"})
            self.assertEqual(result["compute"]["cuda_compute"], "PASS")
            self.assertIn("HostKeyAlias=guest-a", mock.call_args_list[0].args[0])
            self.assertIn("gpu-compute-check.py", mock.call_args_list[1].args[0][-1])
