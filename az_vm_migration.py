#!/usr/bin/env python3
# Author: Umesh Panwar
# Date: June 20, 2024
# Description: Recreate Azure VMs and their managed disks in another availability zone.

import argparse
import csv
import json
import logging
from pathlib import Path
import subprocess
import sys


class AzureCliError(RuntimeError):
    pass


def run_az(*arguments):
    command = ["az", *map(str, arguments), "--only-show-errors", "--output", "json"]
    logging.debug("Running Azure CLI command: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown Azure CLI error"
        raise AzureCliError(f"Azure CLI command failed: {detail}")

    if not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AzureCliError("Azure CLI returned invalid JSON") from error


def capture_subnet(vnet_resource_group, vnet_name, subnet_name):
    vnet = run_az(
        "network",
        "vnet",
        "show",
        "--resource-group",
        vnet_resource_group,
        "--name",
        vnet_name,
    )
    subnet = run_az(
        "network",
        "vnet",
        "subnet",
        "show",
        "--resource-group",
        vnet_resource_group,
        "--vnet-name",
        vnet_name,
        "--name",
        subnet_name,
    )
    logging.info("Using subnet %s", subnet["id"])
    return subnet["id"], vnet["location"]


def get_managed_disk_id(disk):
    disk_id = disk.get("managedDisk", {}).get("id")
    if not disk_id:
        raise ValueError(f"Disk {disk.get('name', '<unknown>')} is not a managed disk")
    return disk_id


def validate_source_vm_state(resource_group, vm_name):
    instance_view = run_az(
        "vm",
        "get-instance-view",
        "--resource-group",
        resource_group,
        "--name",
        vm_name,
    )
    statuses = instance_view.get("instanceView", {}).get("statuses", [])
    power_state = next(
        (
            status.get("code")
            for status in statuses
            if status.get("code", "").startswith("PowerState/")
        ),
        None,
    )
    if power_state != "PowerState/deallocated":
        raise ValueError(
            f"VM {vm_name} must be deallocated before migration; current state is "
            f"{power_state or 'unknown'}"
        )


def create_snapshot(resource_group, location, disk, snapshot_suffix):
    snapshot_name = f"{disk['name']}-{snapshot_suffix}"
    snapshot = run_az(
        "snapshot",
        "create",
        "--resource-group",
        resource_group,
        "--location",
        location,
        "--source",
        get_managed_disk_id(disk),
        "--name",
        snapshot_name,
        "--sku",
        "Standard_ZRS",
    )
    logging.info("Created snapshot %s", snapshot_name)
    return snapshot["id"]


def create_disk_from_snapshot(
    target_resource_group, location, zone, source_disk, snapshot_id
):
    source_disk_details = run_az("disk", "show", "--ids", get_managed_disk_id(source_disk))
    source_sku = source_disk_details.get("sku", {}).get("name")
    if not source_sku:
        raise ValueError(f"Could not determine SKU for disk {source_disk['name']}")

    target_disk_name = f"{source_disk['name']}-zone{zone}"
    target_disk = run_az(
        "disk",
        "create",
        "--resource-group",
        target_resource_group,
        "--location",
        location,
        "--source",
        snapshot_id,
        "--name",
        target_disk_name,
        "--sku",
        source_sku,
        "--zone",
        zone,
    )
    logging.info("Created disk %s", target_disk_name)
    return target_disk["id"]


def attach_data_disks(target_resource_group, target_vm_name, data_disks):
    for source_disk, target_disk_id in sorted(data_disks, key=lambda item: item[0]["lun"]):
        arguments = [
            "vm",
            "disk",
            "attach",
            "--resource-group",
            target_resource_group,
            "--vm-name",
            target_vm_name,
            "--ids",
            target_disk_id,
            "--lun",
            source_disk["lun"],
            "--caching",
            source_disk.get("caching") or "None",
        ]
        if source_disk.get("writeAcceleratorEnabled"):
            arguments.extend(["--enable-write-accelerator", "true"])

        run_az(*arguments)
        logging.info(
            "Attached disk %s to %s at LUN %s",
            source_disk["name"],
            target_vm_name,
            source_disk["lun"],
        )


def migrate_vm(row, subnet_id, subnet_location):
    vm_name, resource_group, csv_os_type, target_resource_group, zone_text, vm_sku = row
    try:
        zone = int(zone_text)
    except ValueError as error:
        raise ValueError(f"Invalid availability zone for VM {vm_name}: {zone_text}") from error
    if zone not in (1, 2, 3):
        raise ValueError(f"Availability zone for VM {vm_name} must be 1, 2, or 3")

    validate_source_vm_state(resource_group, vm_name)
    vm_details = run_az(
        "vm", "show", "--resource-group", resource_group, "--name", vm_name
    )
    location = vm_details["location"]
    if location.casefold() != subnet_location.casefold():
        raise ValueError(
            f"VM {vm_name} is in {location}, but the selected VNet is in {subnet_location}"
        )
    if str(zone) in (vm_details.get("zones") or []):
        raise ValueError(f"VM {vm_name} is already in availability zone {zone}")

    storage_profile = vm_details["storageProfile"]
    os_disk = storage_profile["osDisk"]
    data_disks = storage_profile.get("dataDisks", [])
    os_type = os_disk["osType"]
    if csv_os_type.casefold() != os_type.casefold():
        raise ValueError(
            f"CSV OS type {csv_os_type!r} does not match VM {vm_name} OS type {os_type!r}"
        )

    snapshot_suffix = f"ss-{vm_name}-zone-{zone}"
    os_snapshot_id = create_snapshot(resource_group, location, os_disk, snapshot_suffix)
    target_os_disk_id = create_disk_from_snapshot(
        target_resource_group, location, zone, os_disk, os_snapshot_id
    )

    target_data_disks = []
    for data_disk in data_disks:
        snapshot_id = create_snapshot(
            resource_group, location, data_disk, snapshot_suffix
        )
        target_disk_id = create_disk_from_snapshot(
            target_resource_group, location, zone, data_disk, snapshot_id
        )
        target_data_disks.append((data_disk, target_disk_id))

    target_vm_name = f"{vm_name}-zone{zone}"
    run_az(
        "vm",
        "create",
        "--resource-group",
        target_resource_group,
        "--location",
        location,
        "--name",
        target_vm_name,
        "--attach-os-disk",
        target_os_disk_id,
        "--os-type",
        os_type,
        "--zone",
        zone,
        "--subnet",
        subnet_id,
        "--size",
        vm_sku,
        "--public-ip-address",
        "",
    )
    logging.info("Created VM %s", target_vm_name)
    attach_data_disks(target_resource_group, target_vm_name, target_data_disks)


def read_csv_rows(csv_file):
    with csv_file.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.reader(file)
        if next(reader, None) is None:
            raise ValueError(f"CSV file is empty: {csv_file}")

        found_row = False
        for line_number, raw_row in enumerate(reader, start=2):
            if not raw_row or not any(value.strip() for value in raw_row):
                continue
            if len(raw_row) != 6:
                raise ValueError(
                    f"CSV line {line_number} must have exactly 6 columns; found {len(raw_row)}"
                )
            row = [value.strip() for value in raw_row]
            if not all(row):
                raise ValueError(f"CSV line {line_number} contains an empty required value")
            found_row = True
            yield row

        if not found_row:
            raise ValueError(f"CSV file contains no VM rows: {csv_file}")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Recreate Azure VMs and their managed disks in another availability zone."
    )
    parser.add_argument(
        "csv_file",
        type=Path,
        help="CSV columns: VM name, source resource group, OS type, target resource group, zone, VM SKU",
    )
    parser.add_argument("--vnet-name", required=True)
    parser.add_argument("--subnet-name", required=True)
    parser.add_argument("--vnet-resource-group", required=True)
    parser.add_argument("--log-file", type=Path, default=Path("snapshot.log"))
    return parser.parse_args()


def configure_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )


def main():
    arguments = parse_arguments()
    configure_logging(arguments.log_file)

    try:
        subnet_id, subnet_location = capture_subnet(
            arguments.vnet_resource_group,
            arguments.vnet_name,
            arguments.subnet_name,
        )
        for row in read_csv_rows(arguments.csv_file):
            migrate_vm(row, subnet_id, subnet_location)
    except (AzureCliError, KeyError, OSError, ValueError) as error:
        logging.error("Migration failed: %s", error)
        return 1

    logging.info("All VMs created successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())