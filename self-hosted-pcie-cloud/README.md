# gpu-iaas — single-host GPU and PCIe resource prototype

C++17 daemon owns PCI inventory, IOMMU validation, durable group reservations,
VFIO binding, reset and driver restoration. Python owns scheduling, SQLite
instance/event records, libvirt VM lifecycle and guest verification.

Phase one supports an exclusively assigned GPU plus its audio function.
NIC/NVMe remain visible in inventory. Enabling their assignment requires device
specific host-use checks and reset policies. Web UI, Kubernetes and vGPU are
outside this phase.

## Build and test

Ubuntu dependencies are listed by `bash scripts/setup_host.sh`.
Run it with `--install` to install them. The build itself needs only CMake,
a C++17 compiler and Python 3.10+. Actual VM execution needs python3-libvirt,
QEMU, libvirt, OVMF, cloud-image-utils and OpenSSH.

    cmake -S device-manager -B build
    cmake --build build -j2
    python3 -m unittest discover -s tests -v

Tests launch the actual C++ daemon against a synthetic sysfs tree in /tmp.
They never detach hardware. Unix sockets must be permitted by the environment.

## Read-only inventory and simulation

Default mode can enumerate real sysfs and simulate reservations without
writing any driver attributes. Its readiness state is SIMULATED_READY, distinct
from hardware READY. Use a private local Linux directory for journals and sockets.

    mkdir -p /tmp/gpu-iaas-demo
    ./build/pcie-device-daemon /tmp/gpu-iaas-demo/device.sock \
      /tmp/gpu-iaas-demo/devices configs/devices.allowlist /sys

In another terminal, using the same Unix uid:

    python3 -m controller.main devices
    python3 -m controller.main status

The shipped allowlist is empty. To simulate the observed lab401-wk group, copy
the allowlist and uncomment both 0000:01:00.0 and 0000:01:00.1, then launch the
daemon with that file. Edit instance.example.json and replace the public key.
A simulation image path need not exist.

    python3 -m controller.main create --request-id demo-1 --spec configs/instance.example.json
    python3 -m controller.main stop INSTANCE_UUID

A successful simulation validates inventory/reservation/orchestration only.
It does not boot a VM or execute nvidia-smi.

## Real host prerequisites

1. Inspect IOMMU with scripts/setup_iommu.sh. Review full group membership.
2. Confirm GPU is not boot VGA, has no display/compute users, and is dedicated
   to this service. Stop competing PCI managers and prevent independent device
   binding or VM changes while this service owns the pool.
3. Review both GPU functions in the allowlist. Whole groups must be allowlisted.
   The daemon refuses non-GPU/audio classes and boot VGA.
4. Enable libvirt qemu:///system and its default NAT network. Ensure KVM works.
5. Prepare an allowlisted qcow2 base image with cloud-init, SSH and a working
   NVIDIA driver. The guest login is gpu. A separate overlay is created per VM.
6. Prepare a host SSH private key and matching guest public key. Provision the
   guest SSH host key through a trusted image/provisioning path into known_hosts.
   Strict host-key validation remains enabled. Host-key enrollment is a separate
   administrator step in this prototype.
7. Check each group function's reset_method supports flr. The initial recovery
   policy permits function-level reset only. Unsupported reset quarantines
   the group. RTX 4090 recovery is not yet hardware validated.
8. Set CPU/RAM capacity to a dedicated host budget after reserving resources
   for host workloads. Scheduling accounts for platform instances only.

For real execution, use separate persistent state from simulation. Set execute
to true in a private controller config, device_socket to /run/gpu-iaas/device.sock,
database to /var/lib/gpu-iaas-controller/controller.sqlite, and set real image,
SSH and capacity paths. Start the daemon with --execute and real /sys as root.
The controller must use the same uid; this prototype is an administrator tool.

The supplied systemd unit is an installation template for /opt/gpu-iaas and
/etc/gpu-iaas/devices.allowlist. Review paths before installing and enabling it.
It is not automatically installed by the setup script.

## Lifecycle and recovery

    REQUESTED -> VALIDATING -> ACQUIRING -> DEVICE_READY
              -> STARTING -> VERIFYING -> READY
              -> STOPPING -> RELEASING -> STOPPED

Errors retain uncertain allocations and produce CLEANUP_REQUIRED. Run stop
to retry cleanup. Shutdown is graceful by default; --force explicitly permits
libvirt destroy after a timeout or guest failure. Stop removes the inactive
libvirt definition, releases hardware and retains disk data. Start/delete
commands are not part of this implementation.

    python3 -m controller.main reconcile
    python3 -m controller.main stop INSTANCE_UUID
    python3 -m controller.main stop INSTANCE_UUID --force

The daemon stores original drivers and group ownership in an atomically
replaced, fsynced journal before mutations. A state-directory flock permits one
daemon per pool; commands are serial and sockets restrict peers to the same uid.
Journal modes prevent mixing simulation and real reservations. All production
daemon instances must share the same configured state directory.

After restart, leases remain reserved. Reconcile observes libvirt and daemon
state, reports orphan leases and marks mismatches CLEANUP_REQUIRED. Recovery
is explicit; there is no automatic resume of interrupted boot or automatic
lease expiration. Do not remove journals to force a resource free.

Acquire failures quarantine the lease. Release verifies identities/topology,
requires no open VFIO/NVIDIA/DRM/iommufd handles, binds any incompletely acquired
functions to VFIO, checks all FLR methods, resets functions and restores original
drivers. Failure preserves QUARANTINED. The idle check is intentionally host-wide.
Original kernel modules must remain available for restoration.

This prototype assumes one administrator-controlled host. /proc checks and
cooperative locks cannot fence independent root processes. Real hardware reset,
GPU reuse, host crashes and hostile multitenant isolation require host acceptance
testing before shared service use.

## Optional administrator REST API

The default interface is CLI. A loopback-only synchronous REST adapter is
available when needed. Configure a random token of at least 32 characters in
GPU_IAAS_API_TOKEN and run:

    python3 -m controller.api.server --config configs/controller.json --port 8080

All requests require Authorization: Bearer TOKEN.

| Method | Path | Body |
|---|---|---|
| GET | /devices | — |
| GET | /instances | — |
| POST | /instances | {"request_id":"...","spec":{...}} |
| POST | /instances/UUID/stop | {"force":false} |

REST requests and CLI operations share the same controller lock. Operations are
synchronous and clients should use sufficient timeouts. Repeating a create
request id returns the recorded operation. It does not repeat a failed boot.

## Acceptance gate on lab401-wk

Run create -> guest nvidia-smi -> stop -> create again -> guest nvidia-smi.
Then exercise competing reservations, daemon/controller restarts, guest boot
failure, shutdown timeout and unsupported reset. Verify no uncertain resource
is returned to the pool. Hardware tests and real nvidia-smi results are pending.

## References

- https://docs.kernel.org/driver-api/vfio.html
- https://libvirt.org/formatdomain.html#host-device-assignment

Domain XML uses hostdev managed="no" because the C++ daemon owns driver changes.
