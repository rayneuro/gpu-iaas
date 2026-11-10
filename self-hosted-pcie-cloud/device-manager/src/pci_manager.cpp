#include "pci_manager.hpp"
#include "iommu.hpp"
#include "vfio.hpp"
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include <fstream>
#include <iomanip>
#include <regex>
#include <sstream>
#include <stdexcept>
namespace pcie {
Manager::Manager(fs::path root, fs::path state, fs::path allowlist, bool execute)
    : root_(std::move(root)), state_(std::move(state)), execute_(execute) {
    fs::create_directories(state_);
    fs::permissions(state_, fs::perms::owner_all, fs::perm_options::replace);
    lock_fd_ = ::open((state_ / "daemon.lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (lock_fd_ < 0 || flock(lock_fd_, LOCK_EX | LOCK_NB) != 0)
        throw std::runtime_error("State directory already locked");
    if (execute_ && (geteuid() != 0 || fs::canonical(root_) != fs::path("/sys")))
        throw std::runtime_error("Execute mode requires root and real /sys");
    std::ifstream config(allowlist);
    if (!config) throw std::runtime_error("Cannot read allowlist");
    std::string line;
    while (std::getline(config, line)) {
        if (!line.empty() && line.front() != '#') allowed_.insert(line);
    }
    load();

}
Manager::~Manager() { if (lock_fd_ >= 0) ::close(lock_fd_); }
void Manager::save() {
    try {
        auto temporary = state_ / "leases.tmp";
        {
            std::ofstream out(temporary);
            if (!out) throw std::runtime_error("Cannot create journal");
            out << (execute_ ? "EXECUTE" : "SIMULATION") << '\n';
            for (const auto& [group, lease] : leases_) {
                out << std::quoted(group) << ' ' << std::quoted(lease.owner) << ' '
                    << std::quoted(lease.state) << ' ' << lease.devices.size() << '\n';
                for (const auto& d : lease.devices)
                    out << std::quoted(d.bdf) << ' ' << std::quoted(d.vendor) << ' '
                        << std::quoted(d.product) << ' ' << std::quoted(d.driver) << '\n';
            }
            out.flush();
            if (!out) throw std::runtime_error("Cannot flush journal");
        }
        int fd = ::open(temporary.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) throw std::runtime_error("Cannot open journal for fsync");
        int result = fsync(fd);
        ::close(fd);
        if (result != 0) throw std::runtime_error("Journal fsync failed");
        fs::rename(temporary, state_ / "leases");
        fd = ::open(state_.c_str(), O_DIRECTORY | O_RDONLY | O_CLOEXEC);
        if (fd < 0) throw std::runtime_error("Cannot open state directory");
        result = fsync(fd);
        ::close(fd);
        if (result != 0) throw std::runtime_error("Directory fsync failed");
    } catch (...) {
        journal_healthy_ = false;
        throw;
    }
}
void Manager::load() {
    if (!fs::exists(state_ / "leases")) return;
    std::ifstream in(state_ / "leases");
    if (!in) throw std::runtime_error("Unreadable lease journal");
    std::string mode;
    if (!(in >> mode) || mode != (execute_ ? "EXECUTE" : "SIMULATION"))
        throw std::runtime_error("Journal execution mode mismatch");
    std::string group;
    while (in >> std::quoted(group)) {
        Lease lease;
        size_t count;
        if (!(in >> std::quoted(lease.owner) >> std::quoted(lease.state) >> count) || count == 0 || count > 256)
            throw std::runtime_error("Corrupt journal");
        for (size_t i = 0; i < count; ++i) {
            Device d;
            d.group = group;
            if (!(in >> std::quoted(d.bdf) >> std::quoted(d.vendor)
                  >> std::quoted(d.product) >> std::quoted(d.driver)))
                throw std::runtime_error("Corrupt device journal");
            lease.devices.push_back(d);
        }
        if (!std::regex_match(group, std::regex("[0-9]+")) || leases_.count(group))
            throw std::runtime_error("Invalid or duplicate group in journal");
        // Restart never silently frees a resource. Recovery is explicit.
        leases_[group] = lease;
    }
    if (!in.eof()) throw std::runtime_error("Corrupt journal");
}
std::string Manager::inventory() {
    std::string out = "{\"ok\":true,\"devices\":[";
    bool first = true;
    for (const auto& entry : fs::directory_iterator(root_ / "bus/pci/devices")) {
        auto d = inspect(root_, entry.path().filename().string());
        if (!first) out += ',';
        first = false;
        out += "{\"bdf\":" + json(d.bdf) + ",\"vendor\":" + json(d.vendor)
            + ",\"product\":" + json(d.product) + ",\"class\":" + json(d.device_class)
            + ",\"driver\":" + json(d.driver) + ",\"group\":" + json(d.group) + "}";
    }
    return out + "]}";
}
std::string Manager::status() {
    std::string out = "{\"ok\":true,\"execute\":" + std::string(execute_ ? "true" : "false") + ",\"leases\":[";
    bool first = true;
    for (const auto& [group, lease] : leases_) {
        if (!first) out += ',';
        first = false;
        out += "{\"group\":" + json(group) + ",\"owner\":" + json(lease.owner)
            + ",\"state\":" + json(lease.state) + "}";
    }
    return out + "]}";
}
std::string Manager::acquire(const std::string& owner, const std::string& bdf) {
    if (!journal_healthy_) throw std::runtime_error("Journal failure; restart and reconcile required");
    if (!std::regex_match(owner, std::regex("[a-zA-Z0-9_-]{1,64}")))
        throw std::runtime_error("Invalid owner");
    auto members = group_members(root_, bdf);
    const auto group = members.front().group;
    auto existing = leases_.find(group);
    if (existing != leases_.end()) {
        if (existing->second.owner != owner) throw std::runtime_error("Group is reserved");
        if (existing->second.state != (execute_ ? "READY" : "SIMULATED_READY"))
            throw std::runtime_error("Lease requires explicit recovery");
        if (execute_) {
            if (members.size() != existing->second.devices.size())
                throw std::runtime_error("Reserved group topology changed");
            for (size_t i = 0; i < members.size(); ++i) {
                const auto& saved = existing->second.devices[i];
                if (members[i].bdf != saved.bdf || members[i].vendor != saved.vendor ||
                    members[i].product != saved.product || members[i].driver != "vfio-pci")
                    throw std::runtime_error("Reserved group requires recovery");
            }
        }
        return "{\"ok\":true,\"state\":" + json(existing->second.state) + "}";
    }
    for (const auto& d : members) {
        if (!allowed_.count(d.bdf)) throw std::runtime_error("Group member not allowlisted: " + d.bdf);
        // Phase one permits GPU and accompanying audio functions only.
        if (d.device_class.rfind("0x03", 0) != 0 && d.device_class != "0x040300")
            throw std::runtime_error("Unsupported device class in GPU group");
        if (d.boot_vga) throw std::runtime_error("Host boot GPU cannot be assigned");
        if (execute_ && !read(root_ / "bus/pci/devices" / d.bdf / "driver_override").empty()
            && read(root_ / "bus/pci/devices" / d.bdf / "driver_override") != "(null)")
            throw std::runtime_error("Existing driver override requires administrator review");
    }
    if (execute_) require_idle();
    leases_[group] = {owner, "BINDING", members};
    save(); // Durable intent and original drivers precede all mutations.
    try {
        if (execute_) {
            for (const auto& d : members) bind_vfio(root_, d);
        }
        leases_[group].state = execute_ ? "READY" : "SIMULATED_READY";
        save();
    } catch (...) {
        leases_[group].state = "QUARANTINED";
        save();
        throw;
    }
    std::string out = "{\"ok\":true,\"group\":" + json(group)
        + ",\"state\":" + json(leases_[group].state) + ",\"devices\":[";
    for (size_t i = 0; i < members.size(); ++i) {
        if (i) out += ',';
        out += json(members[i].bdf);
    }
    return out + "]}";
}
std::string Manager::release(const std::string& owner) {
    if (!journal_healthy_) throw std::runtime_error("Journal failure; restart and reconcile required");
    if (execute_) require_idle();
    // Keep the lease until every device has recovered.
    for (auto it = leases_.begin(); it != leases_.end();) {
        auto& lease = it->second;
        if (lease.owner != owner) { ++it; continue; }
        lease.state = "RELEASING";
        save();
        try {
            if (execute_) {
                auto current = group_members(root_, lease.devices.front().bdf);
                if (current.size() != lease.devices.size())
                    throw std::runtime_error("Group topology changed");
                for (size_t i = 0; i < current.size(); ++i) {
                    const auto& old = lease.devices[i];
                    if (current[i].bdf != old.bdf || current[i].vendor != old.vendor ||
                        current[i].product != old.product || current[i].group != it->first)
                        throw std::runtime_error("Device identity changed");
                }
                // A failed acquire may have bound only part of the group.
                // Finish taking control before attempting any reset.
                for (const auto& d : lease.devices) {
                    if (inspect(root_, d.bdf).driver != "vfio-pci") bind_vfio(root_, d);
                }
                // Preflight every reset method before the first reset.
                for (const auto& d : lease.devices) {
                    if (read(root_ / "bus/pci/devices" / d.bdf / "reset_method").find("flr") == std::string::npos)
                        throw std::runtime_error("Group lacks approved FLR recovery");
                }
                for (const auto& d : lease.devices) reset_device(root_, d);
                for (const auto& d : lease.devices) restore_driver(root_, d);
            }
        } catch (...) {
            lease.state = "QUARANTINED";
            save();
            throw;
        }
        it = leases_.erase(it);
        save();
    }
    return "{\"ok\":true,\"state\":\"RELEASED\"}";
}
}
