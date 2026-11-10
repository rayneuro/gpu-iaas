#include "iommu.hpp"
#include <algorithm>
#include <stdexcept>
namespace pcie {
std::vector<Device> group_members(const fs::path& root, const std::string& bdf) {
    auto d = inspect(root, bdf);
    if (d.group.empty()) throw std::runtime_error("Device has no IOMMU group");
    std::vector<Device> members;
    for (const auto& entry : fs::directory_iterator(root / "kernel/iommu_groups" / d.group / "devices"))
        members.push_back(inspect(root, entry.path().filename().string()));
    std::sort(members.begin(), members.end(), [](const Device& a, const Device& b) { return a.bdf < b.bdf; });
    if (std::none_of(members.begin(), members.end(), [&](const Device& member) { return member.bdf == bdf; }))
        throw std::runtime_error("Inconsistent IOMMU membership");
    return members;
}
}
