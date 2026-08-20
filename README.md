# Azure VM Availability Zone Migration Tool

This Python tool recreates Azure virtual machines in another availability zone by:

1. Taking `Standard_ZRS` snapshots of the source VM's managed OS and data disks.
2. Creating zonal managed disks from those snapshots in the target resource group.
3. Creating a VM from the new OS disk in the requested zone and subnet.
4. Attaching the new data disks with their original LUN, caching policy, and write accelerator setting.

The source VM and its disks are not deleted or modified. The source VM must be deallocated before the tool runs.

## Prerequisites

- Python 3.8 or later.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed and available as `az`.
- An authenticated Azure CLI session with the correct subscription selected.
- RBAC permissions to read the source VM, disks, and VNet, and to create snapshots, disks, NICs, and VMs.
- An existing target resource group, VNet, and subnet.
- Source VMs that use Azure managed disks.
- Target zones and VM sizes that are supported in the source VM's Azure region.
- Regional support for `Standard_ZRS` snapshots.

The selected VNet must be in the same Azure region as every VM in the CSV file. All rows use the same target VNet and subnet.

## CSV Format

The input CSV must contain a header followed by exactly six columns per VM:

| Column | Description | Example |
| --- | --- | --- |
| Source VM Name | Name of the existing VM | `app-vm-01` |
| Source RG | Resource group containing the source VM | `source-rg` |
| OS Type | `Linux` or `Windows`; must match the source VM | `Linux` |
| Target RG | Existing resource group for the new disks and VM | `target-rg` |
| Target Zone | Availability zone `1`, `2`, or `3` | `2` |
| Target VM Size | Azure VM SKU for the new VM | `Standard_D2s_v5` |

Example:

```csv
Source VM Name,Source RG,OS Type,Target RG,Target Zone,Target VM Size
app-vm-01,source-rg,Linux,target-rg,2,Standard_D2s_v5
```

See [test.csv](test.csv) for the included sample.

## Before Running

Sign in and select the intended subscription:

```bash
az login
az account set --subscription "<subscription-name-or-id>"
```

Deallocate each source VM to produce a consistent point-in-time disk snapshot:

```bash
az vm deallocate --resource-group source-rg --name app-vm-01
```

Confirm that the target VM size is available in the intended region and zone, and that the subscription has sufficient quota. The script does not perform SKU or quota preflight checks.

## Usage

Run the tool from this directory:

```bash
python3 az_vm_migration.py test.csv \
  --vnet-name target-vnet \
  --subnet-name target-subnet \
  --vnet-resource-group network-rg
```

To write logs to a different location, add `--log-file`:

```bash
python3 az_vm_migration.py test.csv \
  --vnet-name target-vnet \
  --subnet-name target-subnet \
  --vnet-resource-group network-rg \
  --log-file migration.log
```

Use `python3 az_vm_migration.py --help` to display all arguments.

## Created Resources

For a source VM named `app-vm-01` targeting zone `2`, the tool uses these names:

| Resource | Naming pattern | Example |
| --- | --- | --- |
| VM | `<source-vm>-zone<zone>` | `app-vm-01-zone2` |
| Managed disk | `<source-disk>-zone<zone>` | `app-vm-01-osdisk-zone2` |
| Snapshot | `<source-disk>-ss-<source-vm>-zone-<zone>` | `app-vm-01-osdisk-ss-app-vm-01-zone-2` |

The new VM receives a new NIC in the selected subnet and no public IP address. Progress is written to the console and to `snapshot.log` by default.

## Validation and Failure Behavior

Before creating resources for each row, the tool verifies that:

- The source VM is deallocated.
- The target zone is `1`, `2`, or `3`.
- The requested OS type matches the source VM.
- The source VM and target VNet are in the same region.
- The source VM is not already in the requested zone.
- Every source disk is an Azure managed disk.

The tool stops at the first error and returns a nonzero exit code. It does not roll back snapshots, disks, NICs, or VMs created before the error. Review the log and remove partial resources before retrying. Existing resources with the generated names can also cause a retry to fail.

## Limitations

This is a disk-based VM reconstruction workflow, not a full-fidelity VM configuration migration. It does not copy the source VM's existing NIC, private IP, NSG associations, public IP, managed identity, extensions, tags, boot diagnostics, marketplace plan, availability set, proximity placement group, or security profile.

Review and reapply any required networking, identity, security, monitoring, backup, and extension configuration before placing the new VM into service. Validate the process on a disposable VM before using it for production workloads.

## License

This project is licensed under the [MIT License](LICENSE).
