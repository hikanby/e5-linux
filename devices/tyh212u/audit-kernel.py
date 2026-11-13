#!/usr/bin/env python3
"""Audit a clean pinned kernel checkout without building or contacting a phone.

Only reviewed source directories and exact Kconfig/field tokens are inspected.
Matches are textual source evidence, never proof of DT probing, dependency
resolution, compilation, boot, charging or other hardware functionality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REVISION = '020b970e351eee3887e6caaf2d653926e7086e44'
VERSION = '6.18.54'
HERE = Path(__file__).resolve().parent
SCOPES = (
    'drivers/clk/sprd', 'drivers/pinctrl/sprd', 'drivers/ufs/host',
    'drivers/usb/phy', 'drivers/mfd', 'drivers/regulator',
    'drivers/power/supply', 'drivers/input/touchscreen',
    'drivers/unisoc_platform/sprdwcn',
    'drivers/unisoc_platform/sprd_wlan_combo',
    'drivers/unisoc_platform/sprd_disp', 'drivers/thermal', 'drivers/nvmem',
)
COMPONENTS = (
    ('ums9620_clocks', ('SPRD_UMS9620_CLK',), (), (),
     ('drivers/clk/sprd/ums9620-clk.c',)),
    ('qogirn6pro_pinctrl', ('PINCTRL_SPRD_QOGIRN6PRO',),
     ('sprd,qogirn6pro-pinctrl',), (),
     ('drivers/pinctrl/sprd/pinctrl-sprd-qogirn6pro.c',)),
    ('qogirn6pro_ufs', ('SCSI_UFS_SPRD_QOGIRN6PRO',), ('sprd,ufshc',), (),
     ('drivers/ufs/host/ufs-sprd-qogirn6pro.c',)),
    ('ums9620_usb_phy', ('USB_SPRD_UMS9620_SSPHY',), (), (),
     ('drivers/usb/phy/phy-sprd-ums9620-ssphy.c',)),
    ('pmic_ump9620_family', (),
     ('sprd,ump9620', 'sprd,ump9621', 'sprd,ump9622'), (), ()),
    ('ump9620_regulator', ('REGULATOR_UMP9620',),
     ('sprd,ump962x-regulator', 'sprd,ump9620-regulator'), (),
     ('drivers/regulator/ump9620-regulator.c',)),
    ('sc2355_wifi', ('UNISOC_WCN_BSP', 'UNISOC_WLAN_COMBO', 'SC2355'),
     ('sprd,sc2355-wifi',), (), ()),
    ('sgm41511_charger', ('CHARGER_SGM41511',), ('Sgm,sgm41511_chg',),
     ('sgm41511_chg',), ()),
    ('sc8551_charger', ('CHARGER_SC8551',), ('sc,sc8551-standalone',),
     ('sc8551-charger',), ()),
    ('omnivision_tcm_touch', (), (), ('omnivision_tcm_spi',), ()),
    ('generic_sprd_display', ('UNISOC_DISP',), (),
     ('sprd-mipi-panel-drv',), ('drivers/unisoc_platform/sprd_disp/sprd_drm.c',)),
)
MAX_FILE_BYTES = 8 << 20


class AuditError(ValueError):
    pass


def git(kernel: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', '-c', 'safe.directory=' + str(kernel), '-C', str(kernel), *args],
        capture_output=True, text=True, encoding='utf-8', errors='strict',
        timeout=60, check=False,
    )
    if result.returncode:
        raise AuditError('Kernel Git verification failed')
    return result.stdout.strip()


def verified_checkout(kernel: Path, expected_revision: str = REVISION) -> Path:
    kernel = kernel.resolve(strict=True)
    if not kernel.is_dir():
        raise AuditError('--kernel must be an existing Git checkout directory')
    top = Path(git(kernel, 'rev-parse', '--show-toplevel')).resolve(strict=True)
    if top != kernel:
        raise AuditError('--kernel must identify the checkout root')
    revision = git(kernel, 'rev-parse', 'HEAD')
    if revision != expected_revision:
        raise AuditError('Kernel revision differs from the pinned baseline')
    if git(kernel, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise AuditError('Kernel checkout must be clean before the coverage audit')
    return kernel


def read_source(kernel: Path, relative: str) -> str | None:
    path = kernel / relative
    if not path.exists():
        return None
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(kernel) or path.is_symlink() or not path.is_file():
        raise AuditError('Reviewed source path is a symlink or leaves the checkout')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise AuditError('Reviewed source file exceeds the expected size')
    return path.read_text(encoding='utf-8', errors='strict')


def without_c_comments(text: str) -> str:
    # Preserve string literals and line positions while excluding comments.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)
    return tokens.sub(lambda m: re.sub(r'[^\n]', ' ', m[0])
                      if m[0].startswith('/') else m[0], text)


def source_inventory(kernel: Path) -> dict[str, str]:
    files = {}
    for scope in SCOPES:
        directory = kernel / scope
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.resolve().is_relative_to(kernel):
            raise AuditError('Reviewed source scope leaves the checkout')
        for path in sorted(directory.rglob('*')):
            if path.is_dir():
                if path.is_symlink():
                    raise AuditError('Reviewed source directory contains a symlink')
                continue
            if path.suffix not in ('.c', '.h') and not path.name.startswith('Kconfig') and path.name != 'Makefile':
                continue
            relative = path.relative_to(kernel).as_posix()
            files[relative] = read_source(kernel, relative)
    return files


def index_source(files: dict[str, str]) -> dict:
    index = {'kconfig': {}, 'compatible': {}, 'driver_name': {}, 'makefile_config': {}}

    def add(kind, token, path, text, start):
        index[kind].setdefault(token, []).append(
            {'path': path, 'line': text.count('\n', 0, start) + 1})

    for path, original in sorted(files.items()):
        filename = Path(path).name
        if filename.startswith('Kconfig'):
            for match in re.finditer(r'^[ \t]*(?:menu)?config[ \t]+([A-Z0-9_]+)[ \t]*$', original, re.M):
                add('kconfig', match[1], path, original, match.start())
        elif filename == 'Makefile':
            text = re.sub(r'#[^\n]*', lambda m: ' ' * len(m[0]), original)
            for match in re.finditer(r'CONFIG_([A-Z0-9_]+)', text):
                add('makefile_config', match[1], path, original, match.start())
        else:
            text = without_c_comments(original)
            for field, kind in (('compatible', 'compatible'), ('name', 'driver_name')):
                for match in re.finditer(r'\.' + field + r'\s*=\s*"([^"\\]+)"', text):
                    add(kind, match[1], path, text, match.start())
    return index


def audit_kernel(kernel: Path, *, expected_revision: str = REVISION) -> dict:
    kernel = verified_checkout(kernel, expected_revision)
    makefile = read_source(kernel, 'Makefile')
    if makefile is None:
        raise AuditError('Top-level kernel Makefile is missing')
    values = {}
    for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'):
        matches = re.findall(r'^' + key + r'\s*=\s*(\d+)\s*$', makefile, re.M)
        if len(matches) != 1:
            raise AuditError('Top-level kernel version field is ambiguous')
        values[key] = matches[0]
    version = '.'.join(values[key] for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'))
    if version != VERSION:
        raise AuditError('Kernel Makefile version differs from the pinned baseline')
    files = source_inventory(kernel)
    index = index_source(files)
    results = []
    for name, symbols, compatibles, drivers, paths in COMPONENTS:
        item = {'component': name,
                'kconfig_definitions': {key: index['kconfig'].get(key, []) for key in symbols},
                'makefile_config_references': {key: index['makefile_config'].get(key, []) for key in symbols},
                'exact_compatible_assignments': {key: index['compatible'].get(key, []) for key in compatibles},
                'exact_driver_name_assignments': {key: index['driver_name'].get(key, []) for key in drivers},
                'reviewed_path_presence': {key: key in files for key in paths},
                'compiled': False, 'hardware_validated': False}
        any_match = any(item['reviewed_path_presence'].values()) or any(
            matches for kind in ('kconfig_definitions', 'exact_compatible_assignments',
                                 'exact_driver_name_assignments') for matches in item[kind].values())
        item['coverage_status'] = ('textual_matches_found_review_required' if any_match else
                                   'not_found_in_reviewed_scopes_not_proof_of_absence')
        results.append(item)
    profile_path = HERE / 'profile.json'
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    if profile['kernel_baseline']['revision'] != REVISION:
        raise AuditError('Device profile and audit baseline disagree')
    # Recheck identity/dirty state after scanning; never report a changing tree.
    verified_checkout(kernel, expected_revision)
    return {'schema': 1, 'profile': profile['profile'], 'identity': profile['identity'],
            'audit_kind': 'textual_source_matches_only',
            'kernel_revision': expected_revision, 'kernel_version': version,
            'kernel_checkout_clean': True,
            'profile_sha256': hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            'reviewed_scopes': list(SCOPES), 'reviewed_file_count': len(files),
            'components': results,
            'status': {'target_kernel_built': False, 'bootable': False,
                       'openwrt_built': False, 'openwrt_booted': False,
                       'hardware_validated': False},
            'limitations': [
                'Only exact field literals and Kconfig/Makefile tokens in the listed source scopes are reported',
                'Macros, generated tables, indirect compatible construction and other source scopes may be missed',
                'No arbitrary DT is scanned; only sanitized, independently observed board facts are used',
                'Kconfig presence does not establish enabled configuration, dependency resolution or driver build linkage',
                'No M8 or other board preparation script is applied',
                'No kernel or module compilation, signing, flashing, boot or hardware validation is performed',
                'Missing matches do not prove that the entire source tree lacks a usable driver',
            ]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise AuditError('--out must name a new JSON file')
    result = audit_kernel(args.kernel)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'profile': result['profile'], 'kernel_revision': result['kernel_revision'],
                      'reviewed_file_count': result['reviewed_file_count'],
                      'audit_kind': result['audit_kind'], 'status': result['status']}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AuditError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('Source audit stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
