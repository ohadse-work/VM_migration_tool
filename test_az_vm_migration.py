import unittest

from az_vm_migration import build_vm_template


class BuildVmTemplateTests(unittest.TestCase):
    def test_preserves_all_disk_settings_in_initial_vm_profile(self):
        template = build_vm_template(
            target_vm_name="app-zone2",
            location="eastus",
            zone=2,
            subnet_id="/subscriptions/sub/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/vnet/subnets/default",
            vm_sku="Standard_D4s_v5",
            os_type="Windows",
            source_os_disk={"caching": "ReadWrite"},
            target_os_disk_id="/subscriptions/sub/resourceGroups/target/providers/Microsoft.Compute/disks/os-zone2",
            data_disks=[
                (
                    {
                        "lun": 3,
                        "caching": "None",
                        "writeAcceleratorEnabled": True,
                    },
                    "/subscriptions/sub/resourceGroups/target/providers/Microsoft.Compute/disks/data-3-zone2",
                ),
                (
                    {"lun": 0, "caching": "ReadOnly"},
                    "/subscriptions/sub/resourceGroups/target/providers/Microsoft.Compute/disks/data-0-zone2",
                ),
            ],
        )

        vm = template["resources"][1]
        storage_profile = vm["properties"]["storageProfile"]

        self.assertEqual([0, 3], [disk["lun"] for disk in storage_profile["dataDisks"]])
        self.assertEqual(
            ["ReadOnly", "None"],
            [disk["caching"] for disk in storage_profile["dataDisks"]],
        )
        self.assertEqual(
            [False, True],
            [disk["writeAcceleratorEnabled"] for disk in storage_profile["dataDisks"]],
        )
        self.assertTrue(
            all(
                disk["createOption"] == "Attach"
                and disk["deleteOption"] == "Detach"
                for disk in storage_profile["dataDisks"]
            )
        )
        self.assertEqual("Windows", storage_profile["osDisk"]["osType"])
        self.assertEqual("Attach", storage_profile["osDisk"]["createOption"])


if __name__ == "__main__":
    unittest.main()