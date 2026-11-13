import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'tyh_dwc3', Path(__file__).resolve().parents[1] / 'dwc3.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class Dwc3PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'kernel'
        self.path = self.root / driver.TARGET
        self.path.parent.mkdir(parents=True)
        self.baseline = b'/* baseline */\n' + driver.ANCHOR + b'\n'
        self.prepared = driver.prepare(self.baseline)
        self.path.write_bytes(self.baseline)
        manifest = {
            'path': driver.TARGET,
            'baseline_sha256': hashlib.sha256(self.baseline).hexdigest(),
            'prepared_sha256': hashlib.sha256(self.prepared).hexdigest(),
        }
        self.manifest = self.root / 'port/dwc3-sources.json'
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps(manifest))
        mock_here = patch.object(driver, 'HERE', self.root)
        mock_here.start()
        self.addCleanup(mock_here.stop)

    def test_idempotence_preserves_all_other_source_bytes(self):
        driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.prepared)
        driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.prepared)
        self.assertEqual(self.prepared.replace(driver.ADDITION, b''), self.baseline)

    def test_foreign_or_tampered_prepared_source_is_preserved(self):
        for source in (self.baseline + b'foreign\n', self.prepared + b'foreign\n'):
            self.path.write_bytes(source)
            with self.assertRaisesRegex(ValueError, 'pinned DWC3 baseline'):
                driver.apply(self.root)
            self.assertEqual(self.path.read_bytes(), source)

    def test_prepared_hash_is_checked_before_writes(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['prepared_sha256'] = '0' * 64
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Prepared DWC3 source hash'):
            driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.baseline)

    def test_wrong_table_and_repeated_compatible_are_rejected(self):
        for source in (b'no compatible table', self.baseline * 2, self.prepared):
            with self.assertRaisesRegex(ValueError, 'compatible table'):
                driver.prepare(source)

    def test_linked_destination_is_rejected_without_external_writes(self):
        external = Path(self.temp.name) / 'external.c'
        external.write_bytes(self.baseline)
        self.path.unlink()
        try:
            self.path.symlink_to(external)
        except OSError as error:
            self.skipTest('Host cannot create symbolic links: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'linked DWC3 destination'):
            driver.apply(self.root)
        self.assertEqual(external.read_bytes(), self.baseline)


if __name__ == '__main__':
    unittest.main()
