#include "pci_manager.hpp"
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <iostream>
#include <sstream>
#include <stdexcept>
int main(int argc, char** argv) {
    try {
        if (argc < 5 || argc > 6) throw std::runtime_error(
            "usage: pcie-device-daemon SOCKET STATE_DIR ALLOWLIST SYSFS [--execute]");
        bool execute = argc == 6 && std::string(argv[5]) == "--execute";
        if (argc == 6 && !execute) throw std::runtime_error("Unknown option");
        umask(0077);
        pcie::Manager manager(argv[4], argv[2], argv[3], execute);
        auto socket_path = std::filesystem::path(argv[1]);
        std::filesystem::create_directories(socket_path.parent_path());
        int server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (server < 0) throw std::runtime_error("socket failed");
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        if (std::strlen(argv[1]) >= sizeof(address.sun_path))
            throw std::runtime_error("Socket path too long");
        std::strcpy(address.sun_path, argv[1]);
        // Recover a stale socket only after the state-directory lock is held.
        struct stat socket_info{};
        if (lstat(argv[1], &socket_info) == 0) {
            if (!S_ISSOCK(socket_info.st_mode) || socket_info.st_uid != geteuid())
                throw std::runtime_error("Existing socket path has unexpected ownership/type");
            int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
            if (probe < 0) throw std::runtime_error("Socket probe failed");
            int result = connect(probe, reinterpret_cast<sockaddr*>(&address), sizeof(address));
            int reason = errno;
            close(probe);
            if (result == 0 || reason != ECONNREFUSED)
                throw std::runtime_error("Existing socket may still be active");
            if (unlink(argv[1]) != 0) throw std::runtime_error("Stale socket removal failed");
        }
        if (bind(server, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0)
            throw std::runtime_error("bind failed: " + std::string(strerror(errno)));
        if (listen(server, 16) != 0) throw std::runtime_error("listen failed");
        std::cerr << (execute ? "EXECUTE" : "SIMULATION") << " daemon listening\n";
        for (;;) {
            int client = accept4(server, nullptr, nullptr, SOCK_CLOEXEC);
            if (client < 0) { if (errno == EINTR) continue; throw std::runtime_error("accept failed"); }
            timeval timeout{5, 0};
            setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
            setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
            std::string response;
            try {
                ucred credentials{};
                socklen_t length = sizeof(credentials);
                if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &credentials, &length) != 0 ||
                    credentials.uid != geteuid())
                    throw std::runtime_error("Peer uid rejected");
                std::string request;
                char c;
                while (request.size() < 4096 && recv(client, &c, 1, 0) == 1 && c != '\n')
                    request += c;
                std::istringstream input(request);
                std::string command, owner, bdf, extra;
                input >> command;
                if (command == "LIST" || command == "STATUS") {
                    if (input >> extra) throw std::runtime_error("Unexpected arguments");
                    response = command == "LIST" ? manager.inventory() : manager.status();
                } else if (command == "ACQUIRE") {
                    if (!(input >> owner >> bdf) || input >> extra)
                        throw std::runtime_error("ACQUIRE needs owner and PCI address");
                    response = manager.acquire(owner, bdf);
                } else if (command == "RELEASE") {
                    if (!(input >> owner) || input >> extra)
                        throw std::runtime_error("RELEASE needs owner");
                    response = manager.release(owner);
                } else throw std::runtime_error("Unknown command");
            } catch (const std::exception& error) {
                response = "{\"ok\":false,\"error\":" + pcie::json(error.what()) + "}";
            }
            response += '\n';
            size_t sent = 0;
            while (sent < response.size()) {
                auto count = send(client, response.data() + sent, response.size() - sent, MSG_NOSIGNAL);
                if (count <= 0) break;
                sent += static_cast<size_t>(count);
            }
            close(client);
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
