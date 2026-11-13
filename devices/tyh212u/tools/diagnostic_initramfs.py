"""Offline TYH212U RAM diagnostic archive builder; never connects to a phone.

Original standard-library implementation. The newc layout was cross-checked
against the local MIT-licensed E5 builder, but no E5 init or hardware rules are
included. Output is an initramfs only, not a boot image or usable OpenWrt firmware.
ELF and manifest checks cannot establish binary provenance or hardware safety.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct

MAX_BUSYBOX = 64 * 1024 * 1024
REQUIRED_APPLETS = frozenset({"[", "cat", "mount", "printf", "readlink", "sh", "sleep", "uname"})

# No interactive shell, Android init, module loading, networking, power control,
# block filesystem mounts, persistent logging, serial identifiers or cmdline.
# Stock CONFIG_DEVTMPFS is disabled: /dev contains only the archive's two
# character nodes in initramfs RAM, without a devtmpfs mount or mdev scan.
INIT = b"""#!/bin/sh
PATH=/bin
export PATH
umask 077
trap '' HUP INT TERM

stop() {
    printf '%s\\n' "Diagnostic stopped: $1"
    while :; do /bin/sleep 3600; done
}

[ -c /dev/console ] || stop 'console unavailable'
exec </dev/null >/dev/console 2>&1
printf '%s\\n' 'TYH212U offline RAM diagnostic; hardware support is unvalidated'
/bin/mount -t proc -o ro,nosuid,nodev,noexec proc /proc || stop 'proc unavailable'
/bin/mount -t sysfs -o ro,nosuid,nodev,noexec sysfs /sys || stop 'sysfs unavailable'
/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,noexec,size=1m tmpfs /run || stop 'RAM log unavailable'

report() {
    printf '%s\\n' 'kernel:'
    /bin/uname -srvm
    printf '%s\\n' 'device-tree model:'
    if [ -r /sys/firmware/devicetree/base/model ]; then
        /bin/cat /sys/firmware/devicetree/base/model
        printf '\\n'
    fi
    printf '%s\\n' 'selected driver bindings:'
    for entry in /sys/bus/i2c/devices/2-006b/driver /sys/bus/i2c/devices/3-0066/driver; do
        if [ -L "$entry" ]; then
            printf '%s -> ' "$entry"
            /bin/readlink "$entry"
        fi
    done
    printf '%s\\n' 'USB device controllers (no gadget configured):'
    for controller in /sys/class/udc/*; do
        [ -e "$controller" ] || continue
        printf '%s\\n' "${controller##*/}"
        if [ -r "$controller/state" ]; then /bin/cat "$controller/state"; fi
    done
}

report > /run/diagnostic.txt || stop 'RAM report failed'
/bin/cat /run/diagnostic.txt
printf '%s\\n' 'Diagnostic finished; remaining idle in RAM'
while :; do /bin/sleep 3600; done
"""


class ArchiveError(ValueError):
    pass


def validate_busybox_elf(data: bytes) -> dict:
    """Conservatively require an ELF64 LE AArch64 static ET_EXEC load image.

    This validates structure only, not whether the supplied program is BusyBox.
    Static PIE is deliberately unsupported because PT_DYNAMIC is rejected.
    """
    if not 64 <= len(data) <= MAX_BUSYBOX:
        raise ArchiveError("BusyBox size is outside the supported range")
    if data[:7] != b"\x7fELF\x02\x01\x01":
        raise ArchiveError("Need ELF64 little-endian version 1")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, entry, phoff, _shoff, _flags, ehsize, phsize, phnum, *_ = header
    if (kind, machine, version, ehsize, phsize) != (2, 183, 1, 64, 56):
        raise ArchiveError("Need static AArch64 ET_EXEC with standard ELF64 headers")
    if not 1 <= phnum <= 128 or phoff < 64 or phoff + phnum * phsize > len(data):
        raise ArchiveError("Invalid program header table")
    entry_in_executable_file = False
    loads = 0
    for index in range(phnum):
        typ, flags, offset, address, _physical, filesz, memsz, align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if typ in (2, 3):
            raise ArchiveError("PT_DYNAMIC/PT_INTERP are not accepted")
        if offset + filesz > len(data) or address + memsz >= 1 << 64:
            raise ArchiveError("ELF segment is out of bounds")
        if typ == 1:
            loads += 1
            if filesz > memsz or (align > 1 and (align & (align - 1) or offset % align != address % align)):
                raise ArchiveError("Invalid load segment size or alignment")
            if flags & 1 and address <= entry < address + filesz:
                entry_in_executable_file = True
    if not loads or not entry_in_executable_file:
        raise ArchiveError("No file-backed executable entry point")
    return {"architecture": "aarch64", "elf_type": "ET_EXEC", "load_segments": loads,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def validate_manifest(manifest: dict, data: bytes) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ArchiveError("Need applet manifest schema 1")
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ArchiveError("Manifest sha256 must contain 64 lowercase hex digits")
    if digest != hashlib.sha256(data).hexdigest():
        raise ArchiveError("Manifest is not bound to this input binary")
    applets = manifest.get("applets")
    if not isinstance(applets, list) or not all(isinstance(a, str) for a in applets):
        raise ArchiveError("Manifest applets must be a string list")
    if not REQUIRED_APPLETS <= set(applets):
        raise ArchiveError("Manifest lacks required applets: " + ", ".join(sorted(REQUIRED_APPLETS - set(applets))))


def entries(busybox: bytes) -> list[tuple[str, int, bytes, tuple[int, int]]]:
    items = [(name, stat.S_IFDIR | 0o755, b"", (0, 0)) for name in ("bin", "dev", "proc", "run", "sys")]
    items += [("init", stat.S_IFREG | 0o755, INIT, (0, 0)),
              ("bin/busybox", stat.S_IFREG | 0o755, busybox, (0, 0))]
    items += [("bin/" + applet, stat.S_IFLNK | 0o777, b"busybox", (0, 0)) for applet in sorted(REQUIRED_APPLETS)]
    items += [("dev/console", stat.S_IFCHR | 0o600, b"", (5, 1)),
              ("dev/null", stat.S_IFCHR | 0o666, b"", (1, 3))]
    return items


def newc_archive(items: list[tuple[str, int, bytes, tuple[int, int]]]) -> bytes:
    result = bytearray()
    for ino, (name, mode, payload, rdev) in enumerate(items + [("TRAILER!!!", 0, b"", (0, 0))], 1):
        encoded = name.encode("ascii") + b"\0"
        values = (ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(payload), 0, 0,
                  rdev[0], rdev[1], len(encoded), 0)
        result.extend(b"070701" + b"".join(f"{v:08x}".encode("ascii") for v in values))
        result.extend(encoded)
        result.extend(bytes(-len(result) % 4))
        result.extend(payload)
        result.extend(bytes(-len(result) % 4))
    result.extend(bytes(-len(result) % 512))
    return bytes(result)


def parse_newc(data: bytes) -> list[tuple[str, int, bytes, tuple[int, int]]]:
    """Read the builder's strict canonical subset, including all padding bytes."""
    offset = 0
    found = []
    seen = set()
    while True:
        start = offset
        header = data[offset:offset + 110]
        if len(header) != 110 or header[:6] != b"070701" or not re.fullmatch(b"[0-9a-f]{104}", header[6:]):
            raise ArchiveError("Invalid newc header")
        numbers = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        ino, mode, uid, gid, nlink, mtime, size, devmaj, devmin, rmaj, rmin, namesize, check = numbers
        if ino != len(found) + 1 or (uid, gid, mtime, devmaj, devmin, check) != (0, 0, 0, 0, 0, 0):
            raise ArchiveError("Noncanonical newc metadata")
        if nlink != (2 if stat.S_ISDIR(mode) else 1) or not 1 <= namesize <= 256:
            raise ArchiveError("Invalid link count or name length")
        namebytes = data[start + 110:start + 110 + namesize]
        if len(namebytes) != namesize or namebytes[-1:] != b"\0" or b"\0" in namebytes[:-1]:
            raise ArchiveError("Invalid newc pathname")
        try:
            name = namebytes[:-1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ArchiveError("Non-ASCII newc pathname") from exc
        rawstart = start + 110 + namesize
        payloadstart = (rawstart + 3) & ~3
        payloadend = payloadstart + size
        offset = (payloadend + 3) & ~3
        if offset > len(data) or any(data[rawstart:payloadstart]) or any(data[payloadend:offset]):
            raise ArchiveError("Truncated payload or nonzero newc padding")
        payload = data[payloadstart:payloadend]
        if name == "TRAILER!!!":
            if mode or size or rmaj or rmin or any(data[offset:]) or len(data) % 512:
                raise ArchiveError("Invalid trailer or archive tail")
            return found
        if not name or name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")) or name in seen:
            raise ArchiveError("Unsafe or duplicate newc pathname")
        parent = str(PurePosixPath(name).parent)
        if parent != "." and not any(n == parent and stat.S_ISDIR(m) for n, m, _, _ in found):
            raise ArchiveError("Parent directory must precede child")
        seen.add(name)
        found.append((name, mode, payload, (rmaj, rmin)))


def build(busybox: bytes, manifest: dict) -> bytes:
    validate_busybox_elf(busybox)
    validate_manifest(manifest, busybox)
    expected = entries(busybox)
    raw = newc_archive(expected)
    if parse_newc(raw) != expected:
        raise ArchiveError("Internal newc verification failed")
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0, compresslevel=9) as compressor:
        compressor.write(raw)
    compressed = stream.getvalue()
    if gzip.decompress(compressed) != raw:
        raise ArchiveError("Internal gzip verification failed")
    return compressed


def write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        output.write(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline gzip/newc RAM diagnostic only; not hardware-validated, not a boot image, and no bootability claim.")
    parser.add_argument("--busybox", type=Path, required=True, help="Trusted static ELF64 LE AArch64 ET_EXEC BusyBox (no PT_DYNAMIC/PT_INTERP)")
    parser.add_argument("--manifest", type=Path, required=True, help='Trusted build manifest: {"schema":1,"sha256":"...","applets":["sh","mount",...]}')
    parser.add_argument("--out", type=Path, required=True, help="New output .cpio.gz file; never overwrites")
    args = parser.parse_args(argv)
    try:
        if args.busybox.stat().st_size > MAX_BUSYBOX:
            raise ArchiveError("BusyBox input exceeds size limit")
        binary = args.busybox.read_bytes()
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        archive = build(binary, manifest)
        write_new(args.out, archive)
        print(json.dumps({"output": str(args.out.resolve()), "bytes": len(archive),
                          "sha256": hashlib.sha256(archive).hexdigest(), "hardware_validated": False,
                          "boot_image": False}, ensure_ascii=False))
    except (ArchiveError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
