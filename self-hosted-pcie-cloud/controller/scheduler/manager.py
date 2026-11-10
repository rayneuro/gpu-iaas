"""Serial lifecycle orchestration. Device daemon owns durable group reservations."""
import json
from pathlib import Path
import re
import uuid

from controller.vm_manager.libvirt_vm import LibvirtVM


class Manager:
    def __init__(self, config, store, devices):
        self.config, self.store, self.devices = config, store, devices
        status = devices.request("STATUS")
        if status["execute"] != config["execute"]:
            raise RuntimeError("Controller and daemon execution modes differ")
        self.vm = LibvirtVM(config) if config["execute"] else None

    def normalize(self, spec):
        if set(spec) != {"gpu", "image", "memory_mib", "vcpus", "ssh_public_key", "network"}:
            raise ValueError("Specification fields do not match the documented schema")
        if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", spec["gpu"]):
            raise ValueError("Invalid GPU address")
        for key, minimum, limit in [("vcpus", 1, self.config["cpu_capacity"]),
                                    ("memory_mib", 512, self.config["memory_capacity_mib"])]:
            if type(spec[key]) is not int or not minimum <= spec[key] <= limit:
                raise ValueError("Invalid " + key)
        if not Path(spec["image"]).is_absolute():
            raise ValueError("Image must use an absolute path")
        if self.config["execute"] and str(Path(spec["image"]).resolve()) not in self.config["allowed_images"]:
            raise ValueError("Image not allowlisted")
        if spec["network"] not in self.config["allowed_networks"]:
            raise ValueError("Network not allowlisted")
        if not spec["ssh_public_key"].startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ValueError("An SSH public key is required")
        return dict(spec)

    def create(self, request_id, specification):
        spec = self.normalize(specification)
        instance = self.store.create(str(uuid.uuid4()), request_id, spec)
        identifier = instance["id"]
        # A retry returns the existing operation. Cleanup is explicit via stop.
        if instance["state"] != "REQUESTED":
            return instance
        try:
            self.store.transition(identifier, "VALIDATING")
            active = [item for item in self.store.list()
                      if item["id"] != identifier and item["state"] not in {"STOPPED", "FAILED"}]
            if sum(item["spec"]["vcpus"] for item in active) + spec["vcpus"] > self.config["cpu_capacity"]:
                raise RuntimeError("CPU capacity exhausted")
            if sum(item["spec"]["memory_mib"] for item in active) + spec["memory_mib"] > self.config["memory_capacity_mib"]:
                raise RuntimeError("Memory capacity exhausted")
            inventory = self.devices.request("LIST")["devices"]
            gpu = next(item for item in inventory if item["bdf"] == spec["gpu"])
            if not gpu["class"].startswith("0x03") or not gpu["group"]:
                raise RuntimeError("Requested device is not an isolated GPU")
            members = sorted(item["bdf"] for item in inventory if item["group"] == gpu["group"])
            if self.vm:
                self.vm.assert_unused(members, identifier)
            self.store.transition(identifier, "ACQUIRING")
            result = self.devices.request("ACQUIRE", identifier, spec["gpu"])
            expected = "READY" if self.config["execute"] else "SIMULATED_READY"
            if result["state"] != expected:
                raise RuntimeError("Device daemon did not report readiness")
            self.store.transition(identifier, "DEVICE_READY")
            if self.vm:
                boot_spec = dict(spec, devices=members, mac="52:54:00:" + ":".join(
                    identifier.replace("-", "")[i:i + 2] for i in (0, 2, 4)))
                self.store.transition(identifier, "STARTING")
                self.vm.boot(identifier, boot_spec)
                self.store.transition(identifier, "VERIFYING")
                evidence = self.vm.verify(identifier, boot_spec)
                self.store.transition(identifier, "READY", json.dumps(evidence))
            else:
                self.store.transition(identifier, "SIMULATED_READY")
        except Exception as error:
            # Keep every uncertain allocation. Stop performs recovery, including
            # acquire responses lost after the daemon has committed its journal.
            self.store.transition(identifier, "CLEANUP_REQUIRED", str(error))
            raise
        return self.store.get(identifier)

    def stop(self, identifier, force=False):
        instance = self.store.get(identifier)
        if instance["state"] == "STOPPED":
            return instance
        self.store.transition(identifier, "STOPPING")
        try:
            if self.vm:
                self.vm.stop(identifier, force)
                inventory = self.devices.request("LIST")["devices"]
                gpu = next(item for item in inventory if item["bdf"] == instance["spec"]["gpu"])
                members = [item["bdf"] for item in inventory if item["group"] == gpu["group"]]
                self.vm.assert_unused(members, identifier)
            self.store.transition(identifier, "RELEASING")
            self.devices.request("RELEASE", identifier)
            self.store.transition(identifier, "STOPPED")
        except Exception as error:
            self.store.transition(identifier, "CLEANUP_REQUIRED", str(error))
            raise
        return self.store.get(identifier)

    def reconcile(self):
        status = self.devices.request("STATUS")
        observations = []
        for item in self.store.list():
            leases = [lease for lease in status["leases"] if lease["owner"] == item["id"]]
            domain = self.vm.domain(item["id"]) if self.vm else None
            active = bool(domain and domain.isActive())
            expected = "READY" if self.config["execute"] else "SIMULATED_READY"
            consistent = (item["state"] == expected and leases and
                          all(lease["state"] == expected for lease in leases) and
                          (active if self.vm else True))
            if item["state"] != "STOPPED" and not consistent:
                self.store.transition(item["id"], "CLEANUP_REQUIRED", "Reconcile requires stop/recovery")
            if item["state"] == "STOPPED" and (active or leases):
                self.store.transition(item["id"], "CLEANUP_REQUIRED", "Unexpected ownership after stop")
            observations.append({"id": item["id"], "active": active, "leases": leases})
        known = {item["id"] for item in self.store.list()}
        return {"instances": observations,
                "orphan_leases": [lease for lease in status["leases"] if lease["owner"] not in known]}
