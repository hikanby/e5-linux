import gzip
import hashlib
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest

from diagnostic_initramfs import (ArchiveError, INIT, MINRAM_INIT, REQUIRED_APPLETS, build, entries,
                                 newc_archive, parse_newc, validate_busybox_elf,
                                 validate_manifest, write_new)


def elf_fixture(program_type=1):
    """Synthetic structure only; this fixture contains no executable program."""
    binary = bytearray(256)
    binary[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", binary, 16, 2, 183, 1, 0x400080, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", binary, 64, program_type, 5, 0, 0x400000, 0x400000, 256, 256, 4096)
    return bytes(binary)


def manifest_for(binary):
    return {"schema": 1, "sha256": hashlib.sha256(binary).hexdigest(), "applets": sorted(REQUIRED_APPLETS)}


class DiagnosticArchiveTests(unittest.TestCase):
    def test_separate_pmsg_archive_and_invalid_variant(self):
        binary = elf_fixture()
        console = build(binary, manifest_for(binary))
        candidate = build(binary, manifest_for(binary), "minram-pmsg")
        self.assertNotEqual(console, candidate)
        recovered = parse_newc(gzip.decompress(candidate))
        self.assertEqual({name: data for name, _, data, _ in recovered}["init"], MINRAM_INIT)
        self.assertEqual({name for name, _, _, _ in recovered if name.startswith("dev/")},
                         {"dev/console", "dev/null"})
        with self.assertRaises(ArchiveError):
            build(binary, manifest_for(binary), "unknown")

    @unittest.skipUnless(os.name == "posix", "requires POSIX shell and pseudo-terminal")
    def test_pmsg_record_function_writes_marker_without_running_init(self):
        import pty
        import select
        import tty
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            function = MINRAM_INIT.decode().split("pmsg_record() {", 1)[1].split("\n}\n", 1)[0]
            script = 'pmsg_sink=$1\npmsg_record() {' + function + '\n}\npmsg_record "test-marker"\n'
            result = subprocess.run(["/bin/sh", "-c", script, "record-test", os.ttyname(slave)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertTrue(select.select([master], [], [], 2)[0])
            self.assertEqual(os.read(master, 1024), b"TYH212U-MINRAM-v1 test-marker\n")
        finally:
            os.close(master)
            os.close(slave)

    @unittest.skipUnless(os.name == "posix", "requires POSIX shell and /dev/full")
    def test_pmsg_missing_node_and_write_failure_report_failure(self):
        function = MINRAM_INIT.decode().split("pmsg_record() {", 1)[1].split("\n}\n", 1)[0]
        script = 'pmsg_sink=$1\npmsg_record() {' + function + '\n}\npmsg_record "test-marker"\n'
        with tempfile.TemporaryDirectory() as directory:
            regular = Path(directory) / "regular"
            regular.write_bytes(b"unchanged")
            for sink, message in ((str(regular), b"PMSG unavailable"),
                                  (str(Path(directory) / "missing"), b"PMSG unavailable"),
                                  ("/dev/full", b"PMSG write failed")):
                with self.subTest(sink=sink):
                    result = subprocess.run(["/bin/sh", "-c", script, "record-test", sink],
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stdout)
            self.assertEqual(regular.read_bytes(), b"unchanged")

    def test_structural_static_aarch64_accepted(self):
        self.assertEqual(validate_busybox_elf(elf_fixture())["architecture"], "aarch64")

    def test_dynamic_and_interpreter_rejected(self):
        for typ in (2, 3):
            with self.subTest(typ=typ), self.assertRaises(ArchiveError):
                validate_busybox_elf(elf_fixture(typ))

    def test_wrong_arch_endian_class_and_exec_type_rejected(self):
        for offset, payload in ((4, b"\x01"), (5, b"\x02"), (16, struct.pack("<H", 3)), (18, struct.pack("<H", 62))):
            binary = bytearray(elf_fixture())
            binary[offset:offset + len(payload)] = payload
            with self.subTest(offset=offset), self.assertRaises(ArchiveError):
                validate_busybox_elf(bytes(binary))

    def test_truncation_segment_overflow_and_entry_rejected(self):
        candidates = [elf_fixture()[:60], elf_fixture()[:100]]
        for offset, value in ((64 + 32, 1000), (24, 0x500000), (64 + 48, 3)):
            binary = bytearray(elf_fixture())
            struct.pack_into("<Q", binary, offset, value)
            candidates.append(bytes(binary))
        for binary in candidates:
            with self.subTest(length=len(binary)), self.assertRaises(ArchiveError):
                validate_busybox_elf(binary)

    def test_sha_bound_manifest_and_required_applets(self):
        binary = elf_fixture()
        manifest = manifest_for(binary)
        validate_manifest(manifest, binary)
        for changed in ({**manifest, "sha256": "0" * 64}, {**manifest, "applets": ["sh"]},
                        {**manifest, "schema": 2}, {**manifest, "applets": [1]}):
            with self.assertRaises(ArchiveError):
                validate_manifest(changed, binary)

    def test_deterministic_gzip_newc_and_content(self):
        binary = elf_fixture()
        first = build(binary, manifest_for(binary))
        self.assertEqual(first, build(binary, manifest_for(binary)))
        self.assertEqual(first[4:8], bytes(4))
        raw = gzip.decompress(first)
        self.assertEqual(len(raw) % 512, 0)
        self.assertEqual(parse_newc(raw), entries(binary))
        self.assertIn(b"TRAILER!!!\0", raw)

    def test_modes_devices_and_link_resolution(self):
        records = {n: (m, d, r) for n, m, d, r in parse_newc(newc_archive(entries(elf_fixture())))}
        self.assertEqual(records["init"][0], stat.S_IFREG | 0o755)
        self.assertEqual(records["bin/busybox"][0], stat.S_IFREG | 0o755)
        self.assertEqual(records["dev/console"], (stat.S_IFCHR | 0o600, b"", (5, 1)))
        self.assertEqual(records["dev/null"], (stat.S_IFCHR | 0o666, b"", (1, 3)))
        for applet in REQUIRED_APPLETS:
            mode, target, _ = records["bin/" + applet]
            self.assertTrue(stat.S_ISLNK(mode))
            self.assertIn("bin/" + target.decode(), records)

    def test_unsafe_duplicate_and_unordered_names_rejected(self):
        for records in ([('/bad', stat.S_IFREG | 0o600, b'', (0, 0))],
                        [('a/../b', stat.S_IFREG | 0o600, b'', (0, 0))],
                        [('bin/sh', stat.S_IFLNK | 0o777, b'busybox', (0, 0))],
                        [('a', stat.S_IFDIR | 0o755, b'', (0, 0))] * 2):
            with self.assertRaises(ArchiveError):
                parse_newc(newc_archive(records))

    def test_truncated_padding_and_archive_tail_rejected(self):
        raw = newc_archive(entries(elf_fixture()))
        first_padding = 110 + len("bin") + 1
        damaged = bytearray(raw)
        # First directory is padded after its NUL-terminated pathname.
        damaged[first_padding] = 1
        for value in (raw[:-1], raw[:125], bytes(damaged), raw + b"x" * 512):
            with self.assertRaises(ArchiveError):
                parse_newc(value)

    def test_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.cpio.gz"
            write_new(path, b"original")
            with self.assertRaises(FileExistsError):
                write_new(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original")

    def test_init_has_only_pseudofs_mounts_and_no_persistent_actions(self):
        text = INIT.decode("ascii")
        mounts = [line for line in text.splitlines() if line.startswith("/bin/mount ")]
        self.assertEqual(len(mounts), 3)
        for filesystem in ("proc", "sysfs", "tmpfs"):
            self.assertEqual(sum(f" -t {filesystem} " in line for line in mounts), 1)
        self.assertNotIn(" -t devtmpfs ", text)
        for forbidden in ("/dev/block", "/dev/sda", "mmcblk", "misc", "userdata", "bootctl", "fastboot",
                          "modprobe", "insmod", "reboot", "poweroff", "mount -a", " /data", " /vendor",
                          "dd ", "mkfs", "swap", "mdev", "setprop", "androidboot", "/proc/cmdline"):
            self.assertNotIn(forbidden, text)
        writes = [line.strip() for line in text.splitlines() if ">" in line and not line.strip().startswith("printf")]
        self.assertEqual(writes, ["exec </dev/null >/dev/console 2>&1", "report > /run/diagnostic.txt || stop 'RAM report failed'"])
        self.assertIn("while :; do /bin/sleep 3600; done", text)


if __name__ == "__main__":
    unittest.main()
