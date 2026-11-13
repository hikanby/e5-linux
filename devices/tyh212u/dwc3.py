"""Compile the stock QogirN6Pro DWC3 parent's generic clock/reset glue.

This compatible entry does not implement vendor role, VBUS or BC1.2 policy.
The stock child already uses snps,dwc3; no DWC3 core alias is introduced.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = 'drivers/usb/dwc3/dwc3-of-simple.c'
ANCHOR = b'\t{ .compatible = "sprd,sc9860-dwc3" },'
ADDITION = b'\n\t{ .compatible = "sprd,qogirn6pro-dwc3" },'


def prepare(source):
    if source.count(ANCHOR) != 1 or ADDITION.strip() in source:
        raise ValueError('Unexpected baseline DWC3 compatible table')
    return source.replace(ANCHOR, ANCHOR + ADDITION)


def apply(kernel):
    kernel = Path(kernel).resolve(strict=True)
    path = kernel
    for part in Path(TARGET).parts:
        path = path / part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Refusing linked DWC3 destination')
    if not path.resolve().is_relative_to(kernel) or not path.is_file():
        raise ValueError('Invalid DWC3 destination')
    manifest = json.loads((HERE / 'port/dwc3-sources.json').read_text(encoding='utf-8'))
    if manifest['path'] != TARGET:
        raise ValueError('Unexpected DWC3 source path')
    original = path.read_bytes()
    checksum = hashlib.sha256(original).hexdigest()
    if checksum == manifest['prepared_sha256']:
        return
    if checksum != manifest['baseline_sha256']:
        raise ValueError('Expected the pinned DWC3 baseline or this exact preparation')
    data = prepare(original)
    if hashlib.sha256(data).hexdigest() != manifest['prepared_sha256']:
        raise ValueError('Prepared DWC3 source hash mismatch')
    path.write_bytes(data)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
