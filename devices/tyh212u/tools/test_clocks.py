import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


DEVICE = Path(__file__).resolve().parents[1]
CLOCKS = runpy.run_path(str(DEVICE / 'clocks.py'))
APPLY = CLOCKS['apply']
MANIFEST = json.loads((DEVICE / 'clock-sources.json').read_text(encoding='utf-8'))


class ClockPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'kernel'
        self.root.mkdir()
        git = ['git', '-c', 'user.name=Clock Recipe Test',
               '-c', 'user.email=clock-recipe@example.invalid',
               '-c', 'commit.gpgsign=false', '-C', str(self.root)]
        subprocess.run(git + ['init', '--quiet', '--template='], check=True,
                       capture_output=True)
        subprocess.run(git + ['commit', '--quiet', '--allow-empty', '-m', 'fixture'],
                       check=True, capture_output=True)
        revision = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
        self.globals_patch = patch.dict(APPLY.__globals__, {'REVISION': revision})
        self.globals_patch.start()
        self.addCleanup(self.globals_patch.stop)
        sprd = self.root / 'drivers/clk/sprd'
        sprd.mkdir(parents=True)
        (sprd / 'Makefile').write_bytes(b'# baseline\nobj-y += existing.o\n')
        (sprd / 'Kconfig').write_bytes(b'# baseline\nconfig EXISTING\n\tbool "Existing"\n')
        (sprd / 'existing.c').write_bytes(b'/* unrelated source */\n')

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob('*')
                if p.is_file() and '.git' not in p.relative_to(self.root).parts}

    def test_install_is_idempotent_and_matches_reviewed_output(self):
        before = self.snapshot()
        APPLY(self.root)
        first = self.snapshot()
        APPLY(self.root)
        self.assertEqual(first, self.snapshot())
        self.assertEqual(first[Path('drivers/clk/sprd/existing.c')],
                         before[Path('drivers/clk/sprd/existing.c')])
        for name in ('drivers/clk/sprd/Kconfig', 'drivers/clk/sprd/Makefile'):
            self.assertTrue(first[Path(name)].startswith(before[Path(name)]))
        for name, digest in MANIFEST['prepared_files'].items():
            self.assertEqual(hashlib.sha256(first[Path(name)]).hexdigest(), digest, name)
            self.assertNotIn(b'\r\n', first[Path(name)])

    def test_tampered_input_is_rejected_before_any_write(self):
        sources = Path(self.temp.name) / 'sources'
        for name in MANIFEST['files']:
            target = sources / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CLOCKS['SOURCE_ROOT'] / name, target)
        with (sources / CLOCKS['DRIVER']).open('ab') as stream:
            stream.write(b'/* tampered */\n')
        before = self.snapshot()
        with patch.dict(APPLY.__globals__, {'SOURCE_ROOT': sources}):
            with self.assertRaisesRegex(ValueError, 'source hash mismatch'):
                APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_foreign_header_is_rejected_before_driver_install(self):
        header = self.root / 'include/dt-bindings/reset/sprd,ums9620-reset.h'
        header.parent.mkdir(parents=True)
        header.write_bytes(b'/* other port */\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting UMS9620 clock destination'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_foreign_build_entry_is_rejected_before_any_write(self):
        makefile = self.root / 'drivers/clk/sprd/Makefile'
        with makefile.open('ab') as stream:
            stream.write(b'obj-$(CONFIG_OTHER_PORT) += ums9620-clk.o\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting UMS9620 clock build entry'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_nested_path_cannot_be_used_as_kernel_root(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'root of the pinned Linux'):
            APPLY(self.root / 'drivers')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
