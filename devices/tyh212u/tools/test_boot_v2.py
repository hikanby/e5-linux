import gzip
import struct
import tempfile
import unittest
from pathlib import Path

from boot_v2 import BootError, HEADER_SIZE, PARTS, align, image_id, parse, repack, write_new


def fixture(recovery=b"overlay", second=b"second"):
    page = 2048
    parts = dict(zip(PARTS, (b"kernel" * 400, gzip.compress(b"newc archive fixture"),
                            second, recovery, b"dtb fixture")))
    head = bytearray(page)
    head[:8] = b"ANDROID!"
    for offset, value in ((8, len(parts["kernel"])), (12, 0x8000),
                          (16, len(parts["ramdisk"])), (20, 0x5400000),
                          (24, len(second)), (36, page), (40, 2),
                          (1632, len(recovery)), (1644, HEADER_SIZE),
                          (1648, len(parts["dtb"]))):
        struct.pack_into("<I", head, offset, value)
    struct.pack_into("<Q", head, 1636,
                     page + sum(align(len(parts[n]), page) for n in PARTS[:3]) if recovery else 0)
    struct.pack_into("<Q", head, 1652, 0x1f00000)
    head[48:56] = b"PRIVATE!"
    head[64:77] = b"SECRET=VALUE!"
    head[576:608] = image_id(parts)
    for name in PARTS:
        head.extend(parts[name])
        head.extend(bytes((-len(head)) % page))
    return bytes(head)


def avb_fixture():
    payload = fixture()
    vbmeta = bytearray(256)
    vbmeta[:4] = b"AVB0"
    footer = struct.pack(">4sIIQQQ28s", b"AVBf", 1, 0, len(payload),
                         len(payload), len(vbmeta), bytes(28))
    return payload + vbmeta + footer


class BootV2Tests(unittest.TestCase):
    def test_noop_is_byte_exact_and_private_fields_hidden(self):
        data = fixture()
        image = parse(data)
        self.assertEqual(repack(image), data)
        self.assertTrue(image.summary()["legacy_image_id_matches"])
        self.assertNotIn("PRIVATE", str(image.summary()))
        self.assertNotIn("SECRET", str(image.summary()))

    def test_unsigned_repack_moves_extra_sections_and_keeps_header_metadata(self):
        original = parse(fixture())
        result = parse(repack(original, kernel=b"bigger" * 3000,
                              ramdisk=gzip.compress(b"new ramdisk")))
        for name in ("second", "recovery_dtbo", "dtb"):
            self.assertEqual(result.parts()[name], original.parts()[name])
        for start, end in ((12, 16), (20, 24), (28, 576), (608, 1632), (1644, 1660)):
            self.assertEqual(result.data[start:end], original.data[start:end])
        self.assertTrue(result.summary()["legacy_image_id_matches"])

    def test_empty_optional_sections(self):
        image = parse(fixture(recovery=b"", second=b""))
        self.assertEqual(image.parts()["recovery_dtbo"], b"")
        self.assertTrue(parse(repack(image, kernel=b"new")).summary()["legacy_image_id_matches"])

    def test_truncated_and_overflow_segments_rejected(self):
        for data in (b"", fixture()[:1659], fixture()[:-1]):
            with self.assertRaises(BootError):
                parse(data)
        bad = bytearray(fixture())
        struct.pack_into("<I", bad, 8, 0xffffffff)
        with self.assertRaises(BootError):
            parse(bytes(bad))

    def test_illegal_page_header_and_recovery_overlap(self):
        for offset, value, fmt in ((36, 1024, "<I"), (36, 3000, "<I"),
                                   (40, 4, "<I"), (1644, 1664, "<I"),
                                   (1636, 2048, "<Q")):
            data = bytearray(fixture())
            struct.pack_into(fmt, data, offset, value)
            with self.assertRaises(BootError):
                parse(bytes(data))

    def test_unknown_padding_and_tail_cannot_be_modified(self):
        padding = bytearray(fixture())
        padding[1700] = 1
        for data in (bytes(padding), fixture() + b"UNKNOWN SIGNATURE"):
            image = parse(data)
            self.assertEqual(repack(image), data)
            with self.assertRaises(BootError):
                repack(image, kernel=b"change")

    def test_avb_ranges_and_modification_rejected(self):
        data = avb_fixture()
        image = parse(data)
        self.assertEqual(image.tail_kind, "avb-metadata")
        self.assertEqual(repack(image), data)
        with self.assertRaises(BootError):
            repack(image, kernel=b"change")
        for index, value in ((12, 1), (20, len(data)), (28, 0xffffffffffffffff)):
            bad = bytearray(data)
            struct.pack_into(">Q", bad, len(bad) - 64 + index, value)
            with self.assertRaises(BootError):
                parse(bytes(bad))

    def test_replacement_must_be_valid_gzip(self):
        image = parse(fixture())
        for value in (b"\x02\x21\x4c\x18lz4", b"\x1f\x8bcorrupt", gzip.compress(b"")):
            with self.assertRaises(BootError):
                repack(image, ramdisk=value)

    def test_unknown_legacy_id_cannot_be_modified(self):
        bad = bytearray(fixture())
        bad[576] ^= 1
        with self.assertRaises(BootError):
            repack(parse(bytes(bad)), kernel=b"change")

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "image.img"
            write_new(output, b"original")
            with self.assertRaises(FileExistsError):
                write_new(output, b"changed")
            self.assertEqual(output.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
