#pragma once
#include <filesystem>
#include <string>
#include <vector>
namespace pcie {
namespace fs = std::filesystem;
struct Device {
    std::string bdf, vendor, product, device_class, driver, group;
    bool boot_vga = false;
};
std::string read(const fs::path& path);
std::string json(const std::string& value);
Device inspect(const fs::path& root, const std::string& bdf);
}
