# GPU IaaS prototype

`python3 gpuctl.py inventory` reads PCI addresses, driver names and IOMMU groups.
`plan` renders a Q35/UEFI libvirt domain with a prebuilt qcow2, virtio
management NIC and allowlisted PCI devices. The default allowlist is empty.
Example after an administrator verifies and allowlists both GPU functions:

    python3 gpuctl.py plan gpu-01 --image /var/lib/libvirt/images/gpu-01.qcow2 \
      --pci 0000:01:00.0 0000:01:00.1 > gpu-01.xml

The plan is XML only. Verify the host GPU workload, SSH NIC, mounted disks,
IOMMU grouping, device reset and libvirt default network before applying it.
Use a distinct qcow2 overlay per VM. Guest images need a configured login
and NVIDIA driver for CUDA. Use virtio for management networking.

Ubuntu packages to check: qemu-kvm, qemu-utils, libvirt-daemon-system,
libvirt-clients, ovmf, pciutils. Optional: virtinst, cloud-image-utils,
cockpit-machines. Connect through qemu:///system. VFIO and IOMMU are kernel features.

Next lifecycle implementation: a privileged worker with narrowly scoped
libvirt access, SQLite for durable per-group reservations, then an authenticated
API. Reserve all groups atomically before provisioning. Track provisioning,
running, releasing and failed states. Reconcile against libvirt on restart.
Only return groups to the pool after shutdown and host-device recovery are
verified. Exclude the SSH management NIC and system/data disks from the pool.
