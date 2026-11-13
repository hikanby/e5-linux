"""Test preparation with compact core fixtures and the real pinned pin table."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


DEVICE = Path(__file__).resolve().parents[1]
PINCTRL = runpy.run_path(str(DEVICE / "pinctrl.py"))
APPLY = PINCTRL["apply"]
MANIFEST = json.loads((DEVICE / "port/pinctrl-sources.json").read_text(
    encoding="utf-8"))
CORE = {
    "drivers/pinctrl/sprd/pinctrl-sprd.c": b"/* compact core fixture */\n",
    "drivers/pinctrl/sprd/pinctrl-sprd.h": b"/* compact API fixture */\n",
}


class PinctrlPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "kernel"
        sprd = self.root / "drivers/pinctrl/sprd"
        sprd.mkdir(parents=True)
        for name, data in CORE.items():
            (self.root / name).write_bytes(data)
        (sprd / "Kconfig").write_bytes(b'# baseline\nconfig EXISTING\n\tbool "Existing"\n')
        (sprd / "Makefile").write_bytes(b"# baseline\nobj-y += existing.o\n")
        (sprd / "existing.c").write_bytes(b"/* unrelated source */\n")

        # Only the core fixture hashes differ. The repository cache source and
        # the expected adapted C output retain their real, reviewed hashes.
        self.manifest = json.loads(json.dumps(MANIFEST))
        self.manifest["kernel_api_files"] = {
            name: hashlib.sha256(data).hexdigest() for name, data in CORE.items()
        }
        recipe = self.base / "recipe"
        (recipe / "port").mkdir(parents=True)
        self.manifest_path = recipe / "port/pinctrl-sources.json"
        self.write_manifest()
        globals_patch = patch.dict(APPLY.__globals__, {"HERE": recipe})
        globals_patch.start()
        self.addCleanup(globals_patch.stop)

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assert_rejected_without_writes(self, message):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, message):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest("Symbolic links unavailable: " + str(error))

    def test_install_is_idempotent_preserves_core_and_matches_reviewed_output(self):
        before = self.snapshot()
        APPLY(self.root)
        first = self.snapshot()
        APPLY(self.root)
        self.assertEqual(first, self.snapshot())
        for name, data in before.items():
            self.assertTrue(first[name].startswith(data), str(name))
        for name, data in CORE.items():
            self.assertEqual(first[Path(name)], data)
        target = first[Path(PINCTRL["TARGET"])]
        self.assertEqual(hashlib.sha256(target).hexdigest(), MANIFEST["adapted_sha256"])
        self.assertNotIn(b"\r\n", target)
        self.assertIn(b"PINCTRL_REG_MISC_OFFSET, true);", target)
        self.assertIn(b'.name = "sprd-qogirn6pro-pinctrl",', target)
        self.assertEqual(first[Path("drivers/pinctrl/sprd/Kconfig")].count(
            b"config PINCTRL_SPRD_QOGIRN6PRO"), 1)
        self.assertEqual(first[Path("drivers/pinctrl/sprd/Makefile")].count(
            b"CONFIG_PINCTRL_SPRD_QOGIRN6PRO"), 1)

    def test_each_changed_core_file_is_rejected_before_any_write(self):
        for name, data in CORE.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data + b"/* changed */\n")
                self.assert_rejected_without_writes("pinned 6.18 pinctrl core")
                path.write_bytes(data)

    def test_foreign_driver_is_rejected_before_any_write(self):
        (self.root / PINCTRL["TARGET"]).write_bytes(b"/* another port */\n")
        self.assert_rejected_without_writes("different QogirN6Pro pinctrl")

    def test_foreign_build_entries_are_rejected_before_any_write(self):
        cases = (
            ("Kconfig", b'config PINCTRL_SPRD_QOGIRN6PRO\n\tbool "Other port"\n'),
            ("Makefile", b"obj-$(CONFIG_PINCTRL_SPRD_QOGIRN6PRO) += other.o\n"),
            ("Makefile", b"obj-$(CONFIG_OTHER_PORT) += pinctrl-sprd-qogirn6pro.o\n"),
        )
        for name, addition in cases:
            with self.subTest(name=name, addition=addition):
                path = self.root / "drivers/pinctrl/sprd" / name
                original = path.read_bytes()
                path.write_bytes(original + addition)
                self.assert_rejected_without_writes("existing pinctrl build entry")
                path.write_bytes(original)

    def test_duplicate_prepared_build_entry_is_rejected_before_any_write(self):
        APPLY(self.root)
        path = self.root / "drivers/pinctrl/sprd/Makefile"
        path.write_bytes(path.read_bytes() + PINCTRL["MAKEFILE"])
        self.assert_rejected_without_writes("existing pinctrl build entry")

    def test_manifest_cannot_redirect_a_core_read_outside_the_kernel(self):
        self.manifest["kernel_api_files"]["../outside.c"] = "0" * 64
        self.write_manifest()
        self.assert_rejected_without_writes("Unexpected pinctrl core source path")

    def test_linked_core_is_rejected_before_any_write(self):
        name = next(iter(CORE))
        outside = self.base / "outside-core.c"
        outside.write_bytes(CORE[name])
        link = self.root / name
        link.unlink()
        self.symlink(link, outside)
        self.assert_rejected_without_writes("linked pinctrl destination")
        self.assertEqual(outside.read_bytes(), CORE[name])

    def test_linked_driver_destination_is_rejected_before_any_write(self):
        outside = self.base / "outside-driver.c"
        original = b"/* preserve outside destination */\n"
        outside.write_bytes(original)
        self.symlink(self.root / PINCTRL["TARGET"], outside)
        self.assert_rejected_without_writes("linked pinctrl destination")
        self.assertEqual(outside.read_bytes(), original)

    def test_linked_destination_directory_is_rejected_before_any_write(self):
        sprd = self.root / "drivers/pinctrl/sprd"
        outside = self.base / "outside-sprd"
        sprd.rename(outside)
        self.symlink(sprd, outside, directory=True)
        before = {p.name: p.read_bytes() for p in outside.iterdir()}
        self.assert_rejected_without_writes("linked pinctrl destination")
        self.assertEqual(before, {p.name: p.read_bytes() for p in outside.iterdir()})


if __name__ == "__main__":
    unittest.main()
