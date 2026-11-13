import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
DEVICE = HERE.parent
TOP = DEVICE.parents[1]


class UfsBindingTests(unittest.TestCase):
    def test_profile_records_the_stock_packed_binding(self):
        profile = json.loads((DEVICE / 'profile.json').read_text())
        storage = profile['observed_hardware_bindings']['storage']
        binding = storage['device_tree_binding']
        self.assertEqual(storage['enabled_dt_compatible'], 'sprd,ufshc')
        self.assertEqual(binding['syscon_style'], 'indexed_syscon_names_and_syscons')
        self.assertFalse(binding['standard_resets_present'])
        self.assertEqual(binding['syscon_specifier_cells'], 2)

        driver = (TOP / 'devices/common/ufs/ufs-sprd-qogirn6pro.c').read_text()
        self.assertIn('of_parse_phandle_with_args(np, "syscons"', driver)
        self.assertIn('of_property_match_string(np, "syscon-names", name)', driver)
        self.assertIn('args.args_count != ARRAY_SIZE(syscon_args)', driver)
        self.assertIn('ufs_sprd_set_reset(host->aon_apb_ufs_rst', driver)
        self.assertIn('ufs_sprd_set_reset(host->ap_ahb_ufs_rst', driver)
        for name in binding['syscon_names']:
            self.assertIn('"' + name + '"', driver)

    def test_shared_ufs_source_manifest_and_install_are_idempotent(self):
        manifest = json.loads((TOP / 'devices/common/ufs/sources.json').read_text())
        source_root = TOP / 'devices/common/ufs'
        for name, expected in manifest['files'].items():
            digest = hashlib.sha256((source_root / name).read_bytes()).hexdigest()
            self.assertEqual(digest, expected.lower(), name)

        apply_ufs = runpy.run_path(str(TOP / 'devices/common/ufs.py'))['apply']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = root / 'drivers/ufs/host'
            host.mkdir(parents=True)
            (host / 'Kconfig').write_text('# baseline\n')
            (host / 'Makefile').write_text('# baseline\n')
            (host / 'ufs-sprd.c').write_text('unrelated driver\n')

            apply_ufs(root)
            first = {path.name: path.read_bytes() for path in host.iterdir()}
            apply_ufs(root)
            self.assertEqual(first, {path.name: path.read_bytes() for path in host.iterdir()})
            self.assertEqual((host / 'ufs-sprd.c').read_text(), 'unrelated driver\n')
            self.assertIn('"sprd,ufshc"', (host / 'ufs-sprd-qogirn6pro.c').read_text())


if __name__ == '__main__':
    unittest.main()
