import json
import runpy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

DEVICE = Path(__file__).resolve().parents[1]
USB = runpy.run_path(str(DEVICE / 'usb.py'))


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class IpaPreparationTests(unittest.TestCase):
    def test_only_ipa_component_is_added_and_repeated_preparation_is_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pm = root / 'drivers/pmdomain'
            pm.mkdir(parents=True)
            (pm / 'Makefile').write_text('# unrelated domain\nobj-y += other/\n')
            (pm / 'Kconfig').write_text('# unrelated domain\n')
            USB['apply'](root)
            first = files(root)
            USB['apply'](root)
            self.assertEqual(first, files(root))
            self.assertIn('obj-y += other/', (pm / 'Makefile').read_text())
            self.assertFalse((root / 'drivers/usb').exists())
            self.assertTrue((pm / 'sprd/ums9620-ipa-sys.c').is_file())

    def test_foreign_prepared_domain_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / USB['TARGET']
            path.parent.mkdir(parents=True)
            original = b'foreign prepared source\n'
            path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, 'foreign IPA'):
                USB['apply'](root)
            self.assertEqual(path.read_bytes(), original)

    def test_build_entry_conflicts_leave_every_file_unchanged(self):
        conflicts = {
            'drivers/pmdomain/Makefile': 'obj-$(CONFIG_FOREIGN) += sprd/\n',
            'drivers/pmdomain/Kconfig': 'config SPRD_UMS9620_IPA_PD\n\tbool "Foreign"\n',
            'drivers/pmdomain/sprd/Makefile': 'obj-$(CONFIG_FOREIGN) += ums9620-ipa-sys.o\n',
        }
        for name, content in conflicts.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text(content, encoding='utf-8')
                before = files(root)
                with self.assertRaisesRegex(ValueError, 'Unexpected existing IPA'):
                    USB['apply'](root)
                self.assertEqual(before, files(root))

    def test_tampered_prepared_source_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            USB['apply'](root)
            path = root / USB['TARGET']
            path.write_bytes(path.read_bytes() + b'/* tampered */\n')
            before = files(root)
            with self.assertRaisesRegex(ValueError, 'foreign IPA'):
                USB['apply'](root)
            self.assertEqual(before, files(root))

    def test_recorded_prepared_digest_is_enforced_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'kernel'
            root.mkdir()
            manifest = json.loads(USB['MANIFEST'].read_text(encoding='utf-8'))
            manifest['prepared_sha256'] = '0' * 64
            path = Path(temp) / 'sources.json'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with mock.patch.dict(USB['source'].__globals__, {'MANIFEST': path}):
                with self.assertRaisesRegex(ValueError, 'Prepared IPA source hash mismatch'):
                    USB['apply'](root)
            self.assertEqual({}, files(root))

    def test_outside_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'kernel'
            root.mkdir()
            with self.assertRaisesRegex(ValueError, 'escapes kernel root'):
                USB['_destination'](root.resolve(), '../outside')
            self.assertEqual({}, files(root))

    def test_linked_destination_is_rejected_without_writes(self):
        for directory_link in (False, True):
            with self.subTest(directory_link=directory_link), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'kernel'
                root.mkdir()
                outside = Path(temp) / 'outside'
                outside.mkdir()
                sentinel = outside / 'sentinel'
                sentinel.write_bytes(b'preserve outside\n')
                if directory_link:
                    link = root / 'drivers'
                    target = outside
                else:
                    link = root / USB['TARGET']
                    link.parent.mkdir(parents=True)
                    target = sentinel
                try:
                    link.symlink_to(target, target_is_directory=directory_link)
                except (NotImplementedError, OSError) as error:
                    self.skipTest('Symlink creation unavailable: ' + str(error))
                before = files(root)
                with self.assertRaisesRegex(ValueError, 'destination link'):
                    USB['apply'](root)
                self.assertEqual(before, files(root))
                self.assertEqual(b'preserve outside\n', sentinel.read_bytes())


if __name__ == '__main__':
    unittest.main()
