import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from image_entry import (
    EntryError, FIRST_HIGH_RESERVED, GAP, HEADER, LOADER_ENTRY, MAX_NATIVE_SPAN,
    NATIVE_ENTRY, inspect_image, load_image, main, verify_component, wrap_image,
    write_component,
)


def fixture(*, size=0x4000, length=4096, offset=0, flags=0xA):
    head = HEADER.pack(0xD503201F, 0x1400001F, offset, size, flags,
                       0, 0, 0, b"ARMd", 0)
    return head + bytes(index % 251 for index in range(length - len(head)))


class ImageEntryTests(unittest.TestCase):
    def test_plan_uses_effective_memory_size_including_unloaded_tail(self):
        source = fixture(size=0x670000)
        result = inspect_image(source)
        self.assertEqual(result["kind"], "tyh212u-entry-plan")
        self.assertEqual(result["input"]["memory_tail_bytes"], 0x670000 - len(source))
        self.assertEqual(result["layout"]["outer_image_size"], 0x7F0000)
        self.assertEqual(result["layout"]["outer_file_bytes"], GAP + len(source))
        self.assertEqual(result["layout"]["outer_occupied_range"],
                         ["0x80080000", "0x80870000"])
        self.assertFalse(any(result["validation_boundary"].values()))
        self.assertFalse(result["method"]["runtime_copy"])
        self.assertEqual(result["method"]["registers_modified_by_stub"], [])

    def test_wrapper_instruction_header_padding_and_source_are_exact(self):
        source = fixture()
        wrapped, result = wrap_image(source)
        words = HEADER.unpack_from(wrapped)
        # Decode the actual instruction as an A64 B immediate, without using
        # the implementation's BRANCH or decoder calculation.
        instruction = int.from_bytes(wrapped[:4], "little")
        self.assertEqual(instruction >> 26, 0b000101)
        signed = (instruction & 0x3FFFFFF) - ((instruction & 0x2000000) << 1)
        self.assertEqual(LOADER_ENTRY + 4 * signed, NATIVE_ENTRY)
        self.assertEqual(words[1:], (0, 0x80000, GAP + 0x4000, 0xA,
                                     0, 0, 0, b"ARMd", 0))
        self.assertEqual(wrapped[64:GAP], bytes(GAP - 64))
        self.assertEqual(wrapped[GAP:], source)
        self.assertEqual(struct.unpack_from("<Q", wrapped, GAP + 8)[0], 0)
        self.assertEqual(result["output"]["sha256"], hashlib.sha256(wrapped).hexdigest())
        self.assertEqual(result["output"]["embedded_payload_sha256"], hashlib.sha256(source).hexdigest())
        self.assertTrue(result["output"]["source_byte_exact_verified"])
        self.assertEqual(result["output"]["decoded_branch_target"], "0x80200000")

    def test_selected_gap_boundary_includes_full_bss_span(self):
        plan = inspect_image(fixture(size=MAX_NATIVE_SPAN))
        self.assertEqual(int(plan["layout"]["outer_occupied_range"][1], 16), FIRST_HIGH_RESERVED)
        with self.assertRaises(EntryError):
            inspect_image(fixture(size=MAX_NATIVE_SPAN + 1))

    def test_truncated_magic_flags_offsets_and_sizes_are_refused(self):
        invalid = [b"", fixture()[:63], fixture(length=64), fixture(offset=0x80000),
                   fixture(flags=0), fixture(flags=0xB), fixture(flags=0x1A),
                   fixture(size=0), fixture(size=4095), fixture(size=(1 << 64) - 1)]
        bad_magic = bytearray(fixture())
        bad_magic[56:60] = b"BAD!"
        invalid.append(bytes(bad_magic))
        for source in invalid:
            with self.subTest(header=source[:64]), self.assertRaises(EntryError):
                inspect_image(source)

    def test_unknown_reserved_and_pe_fields_are_refused(self):
        for offset, fmt in ((32, "<Q"), (40, "<Q"), (48, "<Q"), (60, "<I")):
            source = bytearray(fixture())
            struct.pack_into(fmt, source, offset, 1)
            with self.subTest(offset=offset), self.assertRaises(EntryError):
                inspect_image(bytes(source))

    def test_unknown_or_out_of_file_native_entry_instructions_are_refused(self):
        for offset, word in ((0, 0), (0, 0x91005A4D), (4, 0x9400001F),
                             (4, 0xD503201F), (4, 0x14000000),
                             (4, 0x17FFFFFF), (4, 0x14010000)):
            source = bytearray(fixture())
            struct.pack_into("<I", source, offset, word)
            with self.subTest(offset=offset, word=hex(word)), self.assertRaises(EntryError):
                inspect_image(bytes(source))

    def test_wrapping_an_existing_wrapper_is_refused(self):
        wrapped, _ = wrap_image(fixture())
        with self.assertRaises(EntryError):
            wrap_image(wrapped)

    def test_verification_rejects_modified_outer_header_padding_or_payload(self):
        source = fixture()
        wrapped, _ = wrap_image(source)
        for offset in (0, 4, 8, 16, 24, 32, 56, 60, 64, GAP - 1, GAP + 56):
            modified = bytearray(wrapped)
            modified[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(EntryError):
                verify_component(bytes(modified))
        modified = bytearray(wrapped)
        modified[-1] ^= 1
        with self.assertRaises(EntryError):
            verify_component(bytes(modified), source=source)
        with self.assertRaises(EntryError):
            verify_component(wrapped[:GAP + 63])
        with self.assertRaises(EntryError):
            verify_component(wrapped, source=fixture(size=0x5000))
        self.assertFalse(verify_component(wrapped)["output"]["source_byte_exact_verified"])

    def test_default_cli_prints_plan_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "Image"
            source.write_bytes(fixture())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main([str(source)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["kind"], "tyh212u-entry-plan")
            self.assertEqual(list(folder.iterdir()), [source])

    def test_explicit_cli_wrap_and_verify_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "Image"
            source.write_bytes(fixture())
            output, manifest = folder / "Image.entry", folder / "entry.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["wrap", str(source), "--out", str(output),
                                       "--manifest", str(manifest)]), 0)
            report = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertTrue(report["output"]["source_byte_exact_verified"])
            self.assertEqual(report["output"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["verify", str(output), "--source", str(source)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), report)

    def test_existing_output_manifest_and_aliases_are_not_overwritten(self):
        wrapped, report = wrap_image(fixture())
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output, manifest = folder / "entry", folder / "manifest"
            output.write_bytes(b"keep output")
            with self.assertRaises(FileExistsError):
                write_component(output, manifest, wrapped, report)
            self.assertEqual(output.read_bytes(), b"keep output")
            self.assertFalse(manifest.exists())
            output.unlink()
            manifest.write_bytes(b"keep manifest")
            with self.assertRaises(FileExistsError):
                write_component(output, manifest, wrapped, report)
            self.assertEqual(manifest.read_bytes(), b"keep manifest")
            self.assertFalse(output.exists())
            with self.assertRaises(EntryError):
                write_component(output, folder / "." / "entry", wrapped, report)
            self.assertFalse(output.exists())

    def test_oversized_sparse_input_is_refused_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized"
            with path.open("wb") as stream:
                stream.truncate(MAX_NATIVE_SPAN + 1)
            with self.assertRaises(EntryError):
                load_image(path)


if __name__ == "__main__":
    unittest.main()
