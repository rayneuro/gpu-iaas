#include "pci_device.hpp"
#include <fstream>
#include <iomanip>
#include <regex>
#include <sstream>
#include <stdexcept>
namespace pcie {
std::string read(const fs::path& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("Cannot read " + path.string());
    std::string value;
    std::getline(stream, value);
    return value;
}
std::string json(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c);
        else out << c;
    }
    return out.str() + '"';
}
Device inspect(const fs::path& root, const std::string& bdf) {
    if (!std::regex_match(bdf, std::regex("[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\\.[0-7]")))
        throw std::runtime_error("Invalid PCI address");
    auto path = root / "bus/pci/devices" / bdf;
    Device d;
    d.bdf = bdf;
    d.vendor = read(path / "vendor");
    d.product = read(path / "device");
    d.device_class = read(path / "class");
    if (fs::is_symlink(path / "driver"))
        d.driver = fs::canonical(path / "driver").filename().string();
    if (fs::is_symlink(path / "iommu_group"))
        d.group = fs::canonical(path / "iommu_group").filename().string();
    d.boot_vga = fs::exists(path / "boot_vga") && read(path / "boot_vga") == "1";
    return d;
}
}
