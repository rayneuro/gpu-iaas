#pragma once
#include "pci_device.hpp"
#include <map>
#include <set>
namespace pcie {
struct Lease {
    std::string owner, state;
    std::vector<Device> devices;
};
class Manager {
    fs::path root_, state_;
    std::set<std::string> allowed_;
    std::map<std::string, Lease> leases_;
    bool execute_;
    bool journal_healthy_ = true;
    int lock_fd_ = -1;
    void save();
    void load();
public:
    Manager(fs::path root, fs::path state, fs::path allowlist, bool execute);
    ~Manager();
    std::string inventory();
    std::string acquire(const std::string& owner, const std::string& bdf);
    std::string release(const std::string& owner);
    std::string status();
};
}
