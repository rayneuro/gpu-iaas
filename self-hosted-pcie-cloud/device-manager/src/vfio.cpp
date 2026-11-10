#include "vfio.hpp"
#include <fcntl.h>
#include <unistd.h>
#include <stdexcept>
namespace pcie {
void write_sysfs(const fs::path& path, const std::string& value) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CLOEXEC);
    if (fd < 0) throw std::runtime_error("Cannot open " + path.string());
    auto data = value + "\n";
    auto count = ::write(fd, data.data(), data.size());
    ::close(fd);
    if (count != static_cast<ssize_t>(data.size()))
        throw std::runtime_error("Write failed: " + path.string());
}
void require_idle() {
    // Conservative administrator-only check. Includes VFIO group/device fds,
    // NVIDIA and DRM users. Other privileged managers must be stopped.
    for (const auto& process : fs::directory_iterator("/proc")) {
        auto name = process.path().filename().string();
        if (name.find_first_not_of("0123456789") != std::string::npos) continue;
        std::error_code error;
        auto fds = fs::directory_iterator(process.path() / "fd", error);
        if (error) {
            if (!fs::exists(process.path())) continue;
            throw std::runtime_error("Cannot inspect process fds");
        }
        for (const auto& fd : fds) {
            auto target = fs::read_symlink(fd.path(), error).string();
            if (error) continue; // A descriptor may close during inspection.
            if (target.rfind("/dev/vfio/", 0) == 0 ||
                target.rfind("/dev/nvidia", 0) == 0 ||
                target.rfind("/dev/dri/", 0) == 0 ||
                target == "/dev/iommu")
                throw std::runtime_error("Device access still open: " + target);
        }
    }
}
void bind_vfio(const fs::path& root, const Device& d) {
    auto path = root / "bus/pci/devices" / d.bdf;
    write_sysfs(path / "driver_override", "vfio-pci");
    if (fs::is_symlink(path / "driver")) write_sysfs(path / "driver/unbind", d.bdf);
    write_sysfs(root / "bus/pci/drivers_probe", d.bdf);
    if (inspect(root, d.bdf).driver != "vfio-pci")
        throw std::runtime_error("VFIO binding verification failed");
}
void restore_driver(const fs::path& root, const Device& d) {
    auto path = root / "bus/pci/devices" / d.bdf;
    auto current = inspect(root, d.bdf).driver;
    if (!current.empty() && current != d.driver) write_sysfs(path / "driver/unbind", d.bdf);
    write_sysfs(path / "driver_override", d.driver.empty() ? "" : d.driver);
    if (!d.driver.empty() && inspect(root, d.bdf).driver != d.driver)
        write_sysfs(root / "bus/pci/drivers_probe", d.bdf);
    // Clear temporary override after verifying restoration.
    if (inspect(root, d.bdf).driver != d.driver)
        throw std::runtime_error("Original driver restoration failed");
    write_sysfs(path / "driver_override", "");
}
void reset_device(const fs::path& root, const Device& d) {
    auto path = root / "bus/pci/devices" / d.bdf;
    // Only function-level reset is allowed here. Bus reset needs a separate
    // topology-aware policy and is deliberately rejected.
    auto methods = read(path / "reset_method");
    if (methods.find("flr") == std::string::npos)
        throw std::runtime_error("No approved FLR reset for " + d.bdf);
    write_sysfs(path / "reset_method", "flr");
    write_sysfs(path / "reset", "1");
}
}
