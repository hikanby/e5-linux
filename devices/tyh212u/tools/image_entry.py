"""Offline TYH212U ARM64 Image entry component; no phone or boot-container I/O.

The audited stock Android path loads and enters at 0x80080000. This tool
adapts the padding-and-branch method in upstream/wrap-image.py: a new outer
header branches over padding to an unchanged, text_offset=0 Image at
0x80200000. It performs no runtime copy and the branch changes no registers.

The selected low RAM gap is only a geometry filter. Firmware allocations,
CPU/cache state, final DTB, AVB acceptance and hardware operation remain
unvalidated. Output is an entry component, never boot.img or OpenWrt firmware.
Only the observed little-endian, 4 KiB, non-PE/COFF Image header with NOP+B
entry instructions is accepted. The actual kernel's code is not authenticated.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import struct
import sys


HEADER_BYTES = 64
ALIGNMENT = 0x200000
LOADER_ENTRY = 0x80080000
NATIVE_ENTRY = 0x80200000
GAP = NATIVE_ENTRY - LOADER_ENTRY
OUTER_TEXT_OFFSET = 0x80000
FLAGS = 0xA
# From this phone's stock live DT. These bounds do not cover bootloader
# temporary allocations or prove the replacement kernel's actual DT layout.
LOW_RESERVED_END = 0x80002000
FIRST_HIGH_RESERVED = 0x853F0000
MAX_NATIVE_SPAN = FIRST_HIGH_RESERVED - NATIVE_ENTRY
HEADER = struct.Struct("<IIQQQQQQ4sI")
BRANCH = 0x14000000 | (GAP // 4)


class EntryError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _branch_target(word: int, pc: int) -> int:
    if word & 0xFC000000 != 0x14000000:
        raise EntryError("Need an AArch64 B immediate instruction")
    immediate = word & 0x03FFFFFF
    if immediate & 0x02000000:
        immediate -= 1 << 26
    return pc + (immediate << 2)


def _native_header(data: bytes) -> dict:
    if not HEADER_BYTES <= len(data) <= MAX_NATIVE_SPAN:
        raise EntryError("Native Image file size is outside the selected geometry bounds")
    code0, code1, offset, size, flags, r2, r3, r4, magic, pe = HEADER.unpack_from(data)
    if magic != b"ARMd":
        raise EntryError("Need an uncompressed ARM64 Image with standard magic")
    if offset != 0:
        raise EntryError("Native text_offset must be zero; wrapped/legacy Images are refused")
    if flags != FLAGS:
        raise EntryError("Only the observed flags 0xa (LE, 4 KiB, placement bit set) are supported")
    if r2 or r3 or r4 or pe:
        raise EntryError("Reserved fields and PE/COFF header offset must be zero")
    if code0 != 0xD503201F:
        raise EntryError("Only the observed non-PE/COFF NOP+B entry header is supported")
    primary = _branch_target(code1, 4)
    if not HEADER_BYTES <= primary < len(data):
        raise EntryError("Native entry branch must reach file-backed code after the header")
    if not len(data) <= size <= MAX_NATIVE_SPAN:
        raise EntryError("image_size must cover the file and fit the selected RAM gap")
    return {"format": "ARM64-Image", "file_bytes": len(data),
            "sha256": _digest(data), "text_offset": offset,
            "image_size": size, "flags": flags,
            "primary_entry_file_offset": primary,
            "memory_tail_bytes": size - len(data)}


def inspect_image(data: bytes) -> dict:
    """Return a proposed layout without producing an entry component."""
    native = _native_header(data)
    end = NATIVE_ENTRY + native["image_size"]
    if (LOADER_ENTRY < LOW_RESERVED_END or end > FIRST_HIGH_RESERVED or
            NATIVE_ENTRY % ALIGNMENT or
            (LOADER_ENTRY - OUTER_TEXT_OFFSET) % ALIGNMENT):
        raise EntryError("Entry geometry does not meet the selected bounds/alignment")
    return {
        "schema": 1, "kind": "tyh212u-entry-plan", "model": "TYH212U",
        "input": native,
        "layout": {
            "loader_destination": hex(LOADER_ENTRY),
            "outer_entry": hex(LOADER_ENTRY), "native_entry": hex(NATIVE_ENTRY),
            "prefix_bytes": GAP, "alignment_bytes": ALIGNMENT,
            "outer_text_offset": OUTER_TEXT_OFFSET,
            "outer_image_size": GAP + native["image_size"],
            "outer_file_bytes": GAP + len(data),
            "outer_memory_tail_bytes": native["memory_tail_bytes"],
            "outer_occupied_range": [hex(LOADER_ENTRY), hex(end)],
            "native_occupied_range": [hex(NATIVE_ENTRY), hex(end)],
            "selected_low_reserved_end": hex(LOW_RESERVED_END),
            "selected_high_reserved_start": hex(FIRST_HIGH_RESERVED),
            "selected_gap_geometry_passed": True,
        },
        "method": {"instruction": "AArch64 B immediate", "instruction_word": hex(BRANCH),
                   "runtime_copy": False, "registers_modified_by_stub": []},
        "validation_boundary": {
            "android_boot_image_generated": False, "dtb_generated": False,
            "avb_signed": False, "hardware_tested": False,
            "binary_provenance_verified": False,
            "temporary_loader_memory_validated": False,
            "whole_memory_layout_validated": False,
            "cpu_cache_entry_state_validated": False,
        },
    }


def _outer_header(native: dict) -> bytes:
    return HEADER.pack(BRANCH, 0, OUTER_TEXT_OFFSET, GAP + native["image_size"],
                       native["flags"], 0, 0, 0, b"ARMd", 0)


def wrap_image(data: bytes) -> tuple[bytes, dict]:
    """Create an entry component in memory, retaining every native Image byte."""
    plan = inspect_image(data)
    component = _outer_header(plan["input"]) + bytes(GAP - HEADER_BYTES) + data
    return component, verify_component(component, source=data)


def verify_component(data: bytes, *, source: bytes | None = None) -> dict:
    """Require the exact supported wrapper and optionally a byte-exact source."""
    if not GAP + HEADER_BYTES <= len(data) <= GAP + MAX_NATIVE_SPAN:
        raise EntryError("Entry component file size is outside supported bounds")
    native_data = data[GAP:]
    report = inspect_image(native_data)
    if data[:HEADER_BYTES] != _outer_header(report["input"]):
        raise EntryError("Outer branch/header does not match the native Image layout")
    if any(data[HEADER_BYTES:GAP]):
        raise EntryError("Entry padding is not all zero")
    if source is not None and native_data != source:
        raise EntryError("Embedded native Image differs from the supplied source")
    # The exact word check above establishes B, rather than BL or a register
    # branch. Decode its signed imm26 independently of GAP construction.
    word, = struct.unpack_from("<I", data)
    target = _branch_target(word, LOADER_ENTRY)
    if target != NATIVE_ENTRY:
        raise EntryError("Entry branch does not reach the native Image")
    report["kind"] = "tyh212u-verified-entry-component"
    report["output"] = {"file_bytes": len(data), "sha256": _digest(data),
                        "embedded_payload_sha256": _digest(native_data),
                        "source_byte_exact_verified": source is not None,
                        "decoded_branch_target": hex(target)}
    return report


def load_image(path: Path, *, component: bool = False) -> bytes:
    minimum = GAP + HEADER_BYTES if component else HEADER_BYTES
    maximum = GAP + MAX_NATIVE_SPAN if component else MAX_NATIVE_SPAN
    if not minimum <= path.stat().st_size <= maximum:
        raise EntryError("Input file size is outside supported bounds")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if not minimum <= len(data) <= maximum:
        raise EntryError("Input changed size or exceeds supported bounds")
    return data


def write_component(output: Path, manifest: Path, data: bytes, report: dict) -> None:
    """Create both files exclusively; remove only files created by this call on failure."""
    if output.resolve() == manifest.resolve():
        raise EntryError("Component and manifest paths must be distinct")
    metadata = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    created = []
    try:
        with ExitStack() as stack:
            binary = stack.enter_context(output.open("xb"))
            created.append(output)
            info = stack.enter_context(manifest.open("xb"))
            created.append(manifest)
            binary.write(data)
            info.write(metadata)
    except BaseException:
        for path in reversed(created):
            path.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] not in {"inspect", "wrap", "verify", "-h", "--help"}:
        args_list.insert(0, "inspect")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_cmd = commands.add_parser("inspect", help="Print a plan only; this is the default")
    inspect_cmd.add_argument("image", type=Path)
    wrap_cmd = commands.add_parser("wrap", help="Create a new offline entry component and manifest")
    wrap_cmd.add_argument("image", type=Path)
    wrap_cmd.add_argument("--out", type=Path, required=True)
    wrap_cmd.add_argument("--manifest", type=Path, required=True)
    verify_cmd = commands.add_parser("verify", help="Check an existing component without writing")
    verify_cmd.add_argument("image", type=Path)
    verify_cmd.add_argument("--source", type=Path)
    args = parser.parse_args(args_list)
    data = load_image(args.image, component=args.command == "verify")
    if args.command == "wrap":
        component, report = wrap_image(data)
        write_component(args.out, args.manifest, component, report)
    elif args.command == "verify":
        source = load_image(args.source) if args.source is not None else None
        report = verify_component(data, source=source)
    else:
        report = inspect_image(data)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EntryError, OSError) as error:
        print("Entry component refused: " + str(error), file=sys.stderr)
        raise SystemExit(1)
