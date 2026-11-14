"""Exercise the real C++ reset planner without touching hardware."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ResetPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="reset-policy-build-")
        source = Path(cls.build.name) / "main.cpp"
        source.write_text(r'''#include "vfio.hpp"
#include <iostream>
int main(int argc, char** argv) {
 try {
  std::vector<pcie::Device> devices;
  for (int i=3; i<argc; ++i) devices.push_back(pcie::inspect(argv[1],argv[i]));
  auto target=pcie::group_reset_target(argv[1],devices,argv[2]);
  std::cout << (target.empty()?"FLR":target.string());
  return 0;
 } catch (const std::exception& e) { std::cerr << e.what(); return 1; }
}
''')
        cls.binary = Path(cls.build.name) / "planner"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "device-manager/include"), str(source),
                        str(ROOT / "device-manager/src/vfio.cpp"),
                        str(ROOT / "device-manager/src/pci_device.cpp"),
                        "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reset-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.index = self.root / "bus/pci/devices"
        self.index.mkdir(parents=True)
        self.bridge = self.root / "devices/pci0000:00/0000:00:01.0"
        self.add_device("0000:00:01.0", self.bridge, "0x060400")
        (self.bridge / "reset_subordinate").write_text("")
        self.bdfs = ["0000:01:00.0", "0000:01:00.1"]
        for bdf, kind in zip(self.bdfs, ["0x030000", "0x040300"]):
            self.add_device(bdf, self.bridge / bdf, kind)
        self.approval = self.root / "approved"

    def add_device(self, bdf, path, kind):
        path.mkdir(parents=True)
        for key, value in {"vendor":"0x10de", "device":"0x2684", "class":kind}.items():
            (path / key).write_text(value)
        (self.index / bdf).symlink_to(path)

    def run_plan(self):
        return subprocess.run([str(self.binary), str(self.root), str(self.approval), *self.bdfs],
                              text=True, capture_output=True)

    def approve(self):
        self.approval.write_text("0000:00:01.0\n")

    def test_missing_approval_rejected(self):
        result = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit bridge approval", result.stderr)

    def test_approved_complete_subtree(self):
        self.approve()
        result = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(self.bridge / "reset_subordinate"))

    def test_unleased_sibling_rejected(self):
        self.approve()
        self.add_device("0000:01:00.2", self.bridge / "0000:01:00.2", "0x0c0330")
        result = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the lease", result.stderr)

    def test_unleased_nested_device_rejected(self):
        self.approve()
        self.add_device("0000:02:00.0", self.bridge / "0000:01:01.0/0000:02:00.0", "0x020000")
        self.assertNotEqual(self.run_plan().returncode, 0)

    def test_missing_kernel_interface_rejected(self):
        self.approve()
        (self.bridge / "reset_subordinate").unlink()
        self.assertNotEqual(self.run_plan().returncode, 0)

    def test_all_flr_needs_no_bridge_approval(self):
        for bdf in self.bdfs:
            path = (self.index / bdf).resolve()
            (path / "reset").write_text("")
            (path / "reset_method").write_text("flr bus")
        result = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "FLR")

    def test_flr_token_is_exact(self):
        for bdf in self.bdfs:
            path = (self.index / bdf).resolve()
            (path / "reset").write_text("")
            (path / "reset_method").write_text("notflr bus")
        self.assertNotEqual(self.run_plan().returncode, 0)
