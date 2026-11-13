"""Offline Android boot-v2 inspection and conservative unsigned repacking.

No device commands exist here. Signed/unknown-tail images may only be copied
unchanged. This tool does not sign images or establish hardware bootability.
Format references: AOSP bootimg.h blob 67ae349583e66424e5af0a3b7a386b42b6c958e0
and mkbootimg.py blob ec2958179691a434df917cd1b6f196edaa80e31d, inspected
2026-11-13 in platform/system/tools/mkbootimg. Implementation is independent.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import zlib

MAX_IMAGE = 256 << 20
HEADER_SIZE = 1660
PARTS = ("kernel", "ramdisk", "second", "recovery_dtbo", "dtb")


class BootError(ValueError):
    pass


def align(value: int, page: int) -> int:
    return (value + page - 1) // page * page


def image_id(parts: dict[str, bytes]) -> bytes:
    digest = hashlib.sha1()
    for name in PARTS:
        content = parts[name]
        digest.update(content)
        digest.update(struct.pack("<I", len(content)))
    return digest.digest() + bytes(12)


@dataclass
class BootImage:
    data: bytes
    page: int
    regions: dict[str, tuple[int, int]]
    payload_end: int
    nonzero_padding: bool
    tail_kind: str
    avb: dict | None

    def parts(self) -> dict[str, bytes]:
        return {name: self.data[offset:offset + size]
                for name, (offset, size) in self.regions.items()}

    def summary(self) -> dict:
        return {"format": "Android-boot-v2", "file_bytes": len(self.data),
                "page_bytes": self.page, "header_bytes": HEADER_SIZE,
                "segments": {name: {"offset": offset, "bytes": size}
                             for name, (offset, size) in self.regions.items()},
                "payload_end": self.payload_end,
                "padding_has_nonzero_bytes": self.nonzero_padding,
                "tail_kind": self.tail_kind, "avb": self.avb,
                "legacy_image_id_matches": self.data[576:608] == image_id(self.parts()),
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "signature_verification": "not-performed",
                "hardware_boot_validation": "not-performed"}


def parse(data: bytes) -> BootImage:
    if not HEADER_SIZE <= len(data) <= MAX_IMAGE:
        raise BootError("Image size is outside supported bounds")
    if data[:8] != b"ANDROID!":
        raise BootError("Android boot magic is missing")
    u32 = lambda offset: struct.unpack_from("<I", data, offset)[0]
    if u32(40) != 2 or u32(1644) != HEADER_SIZE:
        raise BootError("Only the exact standard v2 header is supported")
    page = u32(36)
    if page < HEADER_SIZE or page > 65536 or page & (page - 1):
        raise BootError("Invalid boot page size")
    sizes = dict(zip(PARTS, (u32(8), u32(16), u32(24), u32(1632), u32(1648))))
    if not sizes["kernel"] or not sizes["ramdisk"] or not sizes["dtb"]:
        raise BootError("This target-specific tool requires nonempty kernel, ramdisk and DTB")
    position = page
    gaps = [(HEADER_SIZE, page)]
    regions = {}
    recovery_offset = struct.unpack_from("<Q", data, 1636)[0]
    for name in PARTS:
        size = sizes[name]
        if name == "recovery_dtbo":
            if recovery_offset != (position if size else 0):
                raise BootError("Noncanonical or overlapping recovery DTBO offset")
        end = position + size
        padded = align(end, page)
        if position > len(data) or end > len(data) or padded > len(data):
            raise BootError("Segment or alignment padding exceeds image bounds")
        regions[name] = (position, size)
        gaps.append((end, padded))
        position = padded
    nonzero_padding = any(any(data[start:end]) for start, end in gaps)
    tail_kind = "zero" if not any(data[position:]) else "unknown-nonzero"
    avb = None
    if len(data) >= 64 and data[-64:-60] == b"AVBf":
        magic, major, minor, original, offset, size, reserved = struct.unpack(
            ">4sIIQQQ28s", data[-64:])
        if major != 1 or minor != 0 or any(reserved):
            raise BootError("Unsupported AVB footer version or reserved fields")
        if not (position <= original <= offset and size >= 256 and
                offset <= len(data) - 64 and size <= len(data) - 64 - offset):
            raise BootError("AVB ranges overlap payload or exceed image bounds")
        if data[offset:offset + 4] != b"AVB0":
            raise BootError("AVB footer does not point to a vbmeta header")
        auth, auxiliary = struct.unpack_from(">QQ", data, offset + 12)
        if 256 + auth + auxiliary != size:
            raise BootError("AVB vbmeta block sizes disagree with footer")
        avb = {"footer_major": major, "footer_minor": minor,
               "original_image_bytes": original, "vbmeta_offset": offset,
               "vbmeta_bytes": size,
               "algorithm": struct.unpack_from(">I", data, offset + 28)[0]}
        tail_kind = "avb-metadata"
    return BootImage(data, page, regions, position, nonzero_padding, tail_kind, avb)


def load(path: Path) -> BootImage:
    if not HEADER_SIZE <= path.stat().st_size <= MAX_IMAGE:
        raise BootError("Input file size is outside supported bounds")
    return parse(path.read_bytes())


def validate_gzip_ramdisk(data: bytes) -> None:
    if data[:2] != b"\x1f\x8b":
        raise BootError("Replacement ramdisk must be gzip for the observed stock kernel")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            count = 0
            while chunk := stream.read(1 << 20):
                count += len(chunk)
                if count > MAX_IMAGE:
                    raise BootError("Expanded ramdisk exceeds supported size")
    except (OSError, EOFError, zlib.error) as error:
        raise BootError("Replacement gzip ramdisk is corrupt") from error
    if count == 0:
        raise BootError("Replacement gzip ramdisk is empty")


def repack(image: BootImage, *, kernel: bytes | None = None,
           ramdisk: bytes | None = None) -> bytes:
    parts = image.parts()
    updated = dict(parts)
    if kernel is not None:
        updated["kernel"] = kernel
    if ramdisk is not None:
        updated["ramdisk"] = ramdisk
    if updated == parts:
        return image.data
    if image.tail_kind != "zero" or image.nonzero_padding:
        raise BootError("Payload changes refused: AVB/unknown metadata needs an explicit reviewed adapter")
    if image.data[576:608] != image_id(parts):
        raise BootError("Payload changes refused: unrecognized legacy image ID")
    if not updated["kernel"]:
        raise BootError("Replacement kernel cannot be empty")
    if ramdisk is not None:
        validate_gzip_ramdisk(ramdisk)
    if any(len(value) > MAX_IMAGE for value in updated.values()):
        raise BootError("Replacement segment is too large")
    head = bytearray(image.data[:image.page])
    struct.pack_into("<I", head, 8, len(updated["kernel"]))
    struct.pack_into("<I", head, 16, len(updated["ramdisk"]))
    recovery_offset = image.page + sum(align(len(updated[name]), image.page)
                                       for name in PARTS[:3])
    struct.pack_into("<Q", head, 1636, recovery_offset if updated["recovery_dtbo"] else 0)
    head[576:608] = image_id(updated)
    total = image.page + sum(align(len(value), image.page) for value in updated.values())
    if max(total, len(image.data)) > MAX_IMAGE:
        raise BootError("Repacked image exceeds supported size")
    output = head
    for name in PARTS:
        output.extend(updated[name])
        output.extend(bytes((-len(output)) % image.page))
    output.extend(bytes(max(0, len(image.data) - len(output))))
    result = bytes(output)
    parse(result)
    return result


def write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as destination:
        destination.write(content)


def replacement(path: Path | None) -> bytes | None:
    if path is None:
        return None
    if not 0 < path.stat().st_size <= MAX_IMAGE:
        raise BootError("Replacement file size is outside supported bounds")
    return path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("image", type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("image", type=Path)
    extract.add_argument("--out", type=Path, required=True)
    roundtrip = commands.add_parser("roundtrip")
    roundtrip.add_argument("image", type=Path)
    roundtrip.add_argument("--out", type=Path, required=True)
    packing = commands.add_parser("repack")
    packing.add_argument("--stock", type=Path, required=True)
    packing.add_argument("--kernel", type=Path)
    packing.add_argument("--ramdisk", type=Path)
    packing.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    image = load(args.stock if args.command == "repack" else args.image)
    if args.command == "extract":
        args.out.mkdir(parents=False, exist_ok=False)
        for name, data in image.parts().items():
            write_new(args.out / (name + ".bin"), data)
        write_new(args.out / "manifest.json",
                  (json.dumps(image.summary(), indent=2) + "\n").encode())
    elif args.command == "roundtrip":
        write_new(args.out, repack(image))
    elif args.command == "repack":
        kernel = replacement(args.kernel)
        ramdisk = replacement(args.ramdisk)
        output = repack(image, kernel=kernel, ramdisk=ramdisk)
        write_new(args.out, output)
        image = parse(output)
    print(json.dumps(image.summary(), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootError, OSError) as error:
        print(f"Offline operation stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
