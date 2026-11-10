#pragma once
#include "pci_device.hpp"
namespace pcie {
std::vector<Device> group_members(const fs::path& root, const std::string& bdf);
}
