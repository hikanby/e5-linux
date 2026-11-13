"""Stage the QogirN6Pro pin table using the pinned 6.18 pinctrl core."""
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOP = HERE.parents[1]
TARGET = "drivers/pinctrl/sprd/pinctrl-sprd-qogirn6pro.c"
SOURCE = "devices/m8/port/" + TARGET
API_FILES = (
    "drivers/pinctrl/sprd/pinctrl-sprd.c",
    "drivers/pinctrl/sprd/pinctrl-sprd.h",
)

KCONFIG = b"""config PINCTRL_SPRD_QOGIRN6PRO
\ttristate "Unisoc QogirN6Pro pinctrl"
\tdepends on OF
\tdepends on ARCH_SPRD || COMPILE_TEST
\tselect PINCTRL_SPRD
\thelp
\t  Enable the QogirN6Pro pad and peripheral matrix controller.
"""
MAKEFILE = (
    b"obj-$(CONFIG_PINCTRL_SPRD_QOGIRN6PRO) += pinctrl-sprd-qogirn6pro.o\n"
)


def _append(data, marker, addition, name, references=()):
    block = b"\n" + addition
    if marker in data:
        if data.count(marker) != 1 or data.count(block) != 1 or any(
                data.count(ref) != addition.count(ref) for ref in references):
            raise ValueError("Unexpected existing pinctrl build entry: " + name)
        return data
    if any(ref in data for ref in references):
        raise ValueError("Unexpected existing pinctrl build entry: " + name)
    return data + block


def _kernel_path(kernel, name):
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid pinctrl destination path: " + name)
    path = kernel
    for part in relative.parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Refusing linked pinctrl destination: " + name)
    if not path.resolve().is_relative_to(kernel):
        raise ValueError("Pinctrl destination escapes kernel: " + name)
    return path


def apply(kernel):
    kernel = Path(kernel).resolve(strict=True)
    if not kernel.is_dir():
        raise ValueError("Expected a kernel source directory")
    manifest = json.loads((HERE / "port/pinctrl-sources.json").read_text(
        encoding="utf-8"))
    if manifest["repository_cache"] != SOURCE or manifest["source_path"] != TARGET:
        raise ValueError("Unexpected QogirN6Pro pinctrl source path")
    if set(manifest["kernel_api_files"]) != set(API_FILES):
        raise ValueError("Unexpected pinctrl core source path")
    paths = {name: _kernel_path(kernel, name) for name in (
        *API_FILES, TARGET, "drivers/pinctrl/sprd/Kconfig",
        "drivers/pinctrl/sprd/Makefile",
    )}
    source = (TOP / SOURCE).read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest["source_sha256"]:
        raise ValueError("QogirN6Pro pinctrl source checksum mismatch")
    for name, expected in manifest["kernel_api_files"].items():
        if hashlib.sha256(paths[name].read_bytes()).hexdigest() != expected:
            raise ValueError("Expected the pinned 6.18 pinctrl core: " + name)

    for old, new in (
        (b"sprd_pinctrl_core_probe(pdev,", b"sprd_pinctrl_core_probe_ext(pdev,"),
        (b"PINCTRL_REG_MISC_OFFSET);", b"PINCTRL_REG_MISC_OFFSET, true);"),
        (b'.name = "sprd-pinctrl",', b'.name = "sprd-qogirn6pro-pinctrl",'),
    ):
        if source.count(old) != 1:
            raise ValueError("Unexpected upstream QogirN6Pro pinctrl API")
        source = source.replace(old, new)
    if hashlib.sha256(source).hexdigest() != manifest["adapted_sha256"]:
        raise ValueError("Adapted QogirN6Pro pinctrl checksum mismatch")

    target = paths[TARGET]
    if target.exists() and target.read_bytes() != source:
        raise ValueError("Refusing to replace a different QogirN6Pro pinctrl")
    edits = {target: source}
    for name, marker, addition, references in (
        ("drivers/pinctrl/sprd/Kconfig",
         b"config PINCTRL_SPRD_QOGIRN6PRO", KCONFIG, ()),
        ("drivers/pinctrl/sprd/Makefile",
         b"CONFIG_PINCTRL_SPRD_QOGIRN6PRO", MAKEFILE,
         (b"pinctrl-sprd-qogirn6pro.o",)),
    ):
        path = paths[name]
        edits[path] = _append(path.read_bytes(), marker, addition, name, references)
    for path, data in edits.items():
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)


if __name__ == "__main__":
    import sys
    apply(sys.argv[1])
