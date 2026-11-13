#!/usr/bin/env python3
"""Install only the reviewed UMS9620 SoC clocks on the pinned 6.18 baseline.

The three immutable inputs live with the M8 import, but no M8 preparation or
board policy runs here. GPU clock providers are absent from that source and
remain unsupported; compilation is not hardware validation.
"""
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess

HERE = Path(__file__).resolve().parent
REVISION = '020b970e351eee3887e6caaf2d653926e7086e44'
SOURCE_ROOT = HERE.parent / 'm8/port'
DRIVER = 'drivers/clk/sprd/ums9620-clk.c'
AI_GATES = ('powervr_eb', 'mtx_apbreg_eb', 'ai_dvfs_eb', 'ocm_eb',
            'axi_pmon_eb', 'aon_to_ocm_eb', 'nic400_busmon_eb')


def prepare_driver(source):
    # This core separates the legacy ibias PLLs from frequency-table PLLs.
    source = source.replace('SPRD_PLL_HW(', 'SPRD_PLL_FTABLE_HW(')
    source = source.replace('SPRD_PLL_FW_NAME(', 'SPRD_PLL_FTABLE_FW_NAME(')
    # The vendor divider subtracts four; the 6.18 helper adds a signed offset.
    source = runpy.run_path(str(HERE.parent / 'common/clock-offsets.py'))[
        'translate_legacy_offsets'](source)
    # These donor SC setters use ordinary regmap_update_bits with zero offset.
    source = source.replace('SPRD_SC_COMP_CLK_DATA(', 'SPRD_COMP_CLK_DATA(')
    for name in AI_GATES:
        pattern = (r'(static SPRD_SC_GATE_CLK_HW\(' + name +
                   r',\s*"[^"]+",\s*&ai_eb\.common\.hw,\s*0x0,\s*'
                   r'0x1000,\s*BIT\([0-6]\),\s*CLK_IGNORE_UNUSED,\s*)'
                   r'0(\);)')
        source, count = re.subn(pattern, r'\g<1>SPRD_GATE_NON_AON\2', source)
        if count != 1:
            raise ValueError('Unexpected UMS9620 non-AON clock: ' + name)
    anchor = '\t\treset->rcdev.of_node = pdev->dev.of_node;'
    if source.count(anchor) != 1:
        raise ValueError('Unexpected UMS9620 reset registration')
    source = source.replace(anchor, '\t\tspin_lock_init(&reset->lock);\n' + anchor)
    return source


def apply(kernel):
    kernel = Path(kernel).resolve()
    git = ['git', '-c', 'safe.directory=' + str(kernel), '-C', str(kernel)]
    root = Path(subprocess.check_output(git + ['rev-parse', '--show-toplevel'],
                                        text=True).strip()).resolve()
    head = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
    if root != kernel or head != REVISION:
        raise ValueError('Expected the root of the pinned Linux 6.18.54 tree')

    manifest = json.loads((HERE / 'clock-sources.json').read_text(encoding='utf-8'))
    planned = {}
    for name, expected in manifest['files'].items():
        data = (SOURCE_ROOT / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('UMS9620 clock source hash mismatch: ' + name)
        if name == DRIVER:
            data = prepare_driver(data.decode('utf-8')).encode('utf-8')
        if hashlib.sha256(data).hexdigest() != manifest['prepared_files'][name]:
            raise ValueError('UMS9620 clock adaptation hash mismatch: ' + name)
        planned[name] = data

    additions = {
        'drivers/clk/sprd/Makefile': (
            'ums9620-clk.o',
            'obj-$(CONFIG_SPRD_UMS9620_CLK) += ums9620-clk.o\n'),
        'drivers/clk/sprd/Kconfig': (
            'config SPRD_UMS9620_CLK',
            'config SPRD_UMS9620_CLK\n'
            '\tbool "Unisoc UMS9620 clocks"\n'
            '\tdepends on ARM64 && SPRD_COMMON_CLK\n'),
    }

    def target(name):
        path = kernel / name
        if not path.resolve().is_relative_to(kernel):
            raise ValueError('Clock destination escapes the kernel tree: ' + name)
        return path

    # Validate every destination before changing the first file. A tree that
    # carries another port must not silently acquire only part of this recipe.
    for name, data in planned.items():
        path = target(name)
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise ValueError('Conflicting UMS9620 clock destination: ' + name)
    for name, (marker, addition) in additions.items():
        text = target(name).read_text(encoding='utf-8')
        if marker in text:
            if addition not in text:
                raise ValueError('Conflicting UMS9620 clock build entry: ' + name)
            continue
        planned[name] = (text.rstrip('\n') + '\n\n' + addition).encode('utf-8')
    for name, data in planned.items():
        path = target(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
