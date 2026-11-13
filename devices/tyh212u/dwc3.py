"""Prepare generic DWC3 glue and propagate legacy PHY initialization errors.

This compatible entry does not implement vendor role, VBUS or BC1.2 policy.
The stock child already uses snps,dwc3; no DWC3 core alias is introduced.
Each legacy PHY reference retains its original init/shutdown call count.
"""
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
TARGET = 'drivers/usb/dwc3/dwc3-of-simple.c'
CORE_TARGET = 'drivers/usb/dwc3/core.c'
TARGETS = (TARGET, CORE_TARGET)
ANCHOR = b'\t{ .compatible = "sprd,sc9860-dwc3" },'
ADDITION = b'\n\t{ .compatible = "sprd,qogirn6pro-dwc3" },'
CORE_INIT_BEFORE = b'''\tusb_phy_init(dwc->usb2_phy);
\tusb_phy_init(dwc->usb3_phy);'''
CORE_INIT_AFTER = b'''\tret = usb_phy_init(dwc->usb2_phy);
\tif (ret)
\t\treturn ret;

\tret = usb_phy_init(dwc->usb3_phy);
\tif (ret)
\t\tgoto err_shutdown_usb2_phy;'''
CORE_UNWIND_BEFORE = b'''\tusb_phy_shutdown(dwc->usb3_phy);
\tusb_phy_shutdown(dwc->usb2_phy);'''
CORE_UNWIND_AFTER = b'''\tusb_phy_shutdown(dwc->usb3_phy);
err_shutdown_usb2_phy:
\tusb_phy_shutdown(dwc->usb2_phy);'''


def prepare(source):
    if source.count(ANCHOR) != 1 or ADDITION.strip() in source:
        raise ValueError('Unexpected baseline DWC3 compatible table')
    return source.replace(ANCHOR, ANCHOR + ADDITION)


def phy_init_function(source):
    matches = list(re.finditer(
        rb'(?m)^static int dwc3_phy_init\(struct dwc3 \*dwc\)\n\{[\s\S]*?^}\n',
        source))
    if len(matches) != 1:
        raise ValueError('Unexpected baseline DWC3 PHY init function')
    return matches[0]


def prepare_core(source):
    match = phy_init_function(source)
    function = match.group()
    for before, after in ((CORE_INIT_BEFORE, CORE_INIT_AFTER),
                          (CORE_UNWIND_BEFORE, CORE_UNWIND_AFTER)):
        if function.count(before) != 1 or after in function:
            raise ValueError('Unexpected baseline DWC3 PHY init body')
        function = function.replace(before, after)
    return source[:match.start()] + function + source[match.end():]


def _target(kernel, name):
    path = kernel
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Refusing linked DWC3 destination')
    if not path.resolve().is_relative_to(kernel) or not path.is_file():
        raise ValueError('Invalid DWC3 destination')
    return path


def apply(kernel):
    kernel = Path(kernel)
    if kernel.is_symlink() or getattr(kernel, 'is_junction', lambda: False)():
        raise ValueError('Refusing linked DWC3 kernel root')
    kernel = kernel.resolve(strict=True)
    manifest = json.loads((HERE / 'port/dwc3-sources.json').read_text(encoding='utf-8'))
    if (set(manifest['files']) != set(TARGETS) or
            manifest['prepared_files'] != {
                name: record['prepared_sha256']
                for name, record in manifest['files'].items()}):
        raise ValueError('Unexpected DWC3 source paths')
    planned = {}
    for name, transform in ((TARGET, prepare), (CORE_TARGET, prepare_core)):
        path = _target(kernel, name)
        record = manifest['files'][name]
        original = path.read_bytes()
        checksum = hashlib.sha256(original).hexdigest()
        if checksum == record['prepared_sha256']:
            continue
        if checksum != record['baseline_sha256']:
            raise ValueError('Expected the pinned DWC3 baseline or this exact preparation: ' + name)
        data = transform(original)
        if hashlib.sha256(data).hexdigest() != record['prepared_sha256']:
            raise ValueError('Prepared DWC3 source hash mismatch: ' + name)
        planned[path] = data
    for path, data in planned.items():
        path.write_bytes(data)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
