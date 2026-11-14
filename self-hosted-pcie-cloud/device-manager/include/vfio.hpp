#pragma once
#include "pci_device.hpp"
namespace pcie {
void write_sysfs(const fs::path& path, const std::string& value);
void require_idle();
void bind_vfio(const fs::path& root, const Device& device);
void restore_driver(const fs::path& root, const Device& device);
fs::path group_reset_target(const fs::path& root, const std::vector<Device>& devices,
                            const fs::path& approvals);
void reset_device(const fs::path& root, const Device& device);
}
