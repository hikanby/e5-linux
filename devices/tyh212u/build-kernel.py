#!/usr/bin/env python3
"""Compile an experimental ARM64 platform Image and its complete selected modules.

Run via tools/build-release.py run --command-only for workspace locking/logging.
This entry point does not create a DTB, boot image or installable firmware.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import struct
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REVISION = '020b970e351eee3887e6caaf2d653926e7086e44'
PREFIX = 'aarch64-linux-gnu-'
REQUIRED_OBJECTS = (
    'drivers/clk/sprd/ums9620-clk.o',
    'drivers/pinctrl/sprd/pinctrl-sprd-qogirn6pro.o',
    'drivers/ufs/host/ufs-sprd-qogirn6pro.o',
    'drivers/pmdomain/sprd/ums9620-ipa-sys.o',
    'drivers/usb/phy/phy-sprd-ums9620-ssphy.o',
    'drivers/usb/dwc3/dwc3.o',
    'drivers/usb/dwc3/dwc3-of-simple.o',
    'drivers/extcon/extcon-usb-gpio.o',
)
BOUNDARY = {
    'artifact_kind': 'experimental-platform-kernel-compile-only',
    'kernel_image_built': True,
    'complete_selected_modules_built': True,
    'board_dtb_built': False,
    'boot_image_built': False,
    'openwrt_firmware_built': False,
    'phone_contacted': False,
    'hardware_validated': False,
    'usb_phy_and_board_policy_adapted': False,
    'usb_phy_driver_compiled': True,
    'usb_board_role_policy_adapted': False,
    'stock_current_device_mode_dwc3_binding_observed': True,
    'usb_controller_mux_hardware_validated': False,
    'pmic_eic_binding_adapted': False,
    'dwc3_legacy_phy_init_error_handling_adapted': True,
    'pmic_vbus_event_chain_hardware_validated': False,
    'instructions': 'Do not flash. This is not a boot image or an installable OpenWrt release.',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'safe.directory=' + str(root), '-C', str(root), *args])


def config_values(text):
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r'CONFIG_([A-Z0-9_]+)=(.+)', line)
        disabled = re.fullmatch(r'# CONFIG_([A-Z0-9_]+) is not set', line)
        if match:
            name, value = match.groups()
        elif disabled:
            name, value = disabled.group(1), 'n'
        else:
            continue
        if name in values:
            raise ValueError('Duplicate kernel configuration: ' + name)
        values[name] = value
    return values


def verify_config(seed, resolved):
    actual = config_values(resolved)
    mismatches = [name + '=' + value for name, value in config_values(seed).items()
                  if actual.get(name, 'n') != value]
    if mismatches:
        raise ValueError('Kconfig dropped required settings: ' + ', '.join(mismatches))


def relative_file(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not name or '\\' in name:
        raise ValueError('Invalid relative build path')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise ValueError('Build path escapes its explicit directory')
    if not target.is_file():
        raise ValueError('Missing build input: ' + name)
    return target


def changed_sources(kernel):
    names = (git(kernel, 'diff', '--name-only', '-z') +
             git(kernel, 'ls-files', '--others', '--exclude-standard', '-z'))
    paths = sorted(set(name for name in names.decode().split('\0') if name))
    return {name: digest(relative_file(kernel, name)) for name in paths}


def selected_modules(build):
    # Linux 6.18 records relative .o names; modfinal links the matching .ko.
    names = relative_file(build, 'modules.order').read_text().splitlines()
    if not names or len(set(names)) != len(names):
        raise ValueError('Missing or duplicate selected module cohort')
    modules = {}
    for name in names:
        if not name.endswith('.o') or name.startswith('kernel/'):
            raise ValueError('Unexpected modules.order entry: ' + name)
        relative_file(build, name)
        linked = str(PurePosixPath(name).with_suffix('.ko'))
        modules[linked] = relative_file(build, linked)
    return modules


def build_environment():
    env = os.environ.copy()
    for name in ('CFLAGS', 'CPPFLAGS', 'LDFLAGS', 'KCFLAGS', 'KCPPFLAGS',
                 'MAKEFLAGS', 'MFLAGS', 'KBUILD_SRC', 'KBUILD_OUTPUT',
                 'KBUILD_EXTRA_SYMBOLS', 'KBUILD_MODPOST_NOFINAL', 'KBUILD_MODPOST_WARN',
                 'KBUILD_EXTRA_WARN', 'KCONFIG_CONFIG', 'KCONFIG_ALLCONFIG',
                 'CROSS_COMPILE', 'ARCH', 'CC', 'HOSTCC', 'LD', 'AR', 'NM',
                 'OBJCOPY', 'LLVM', 'LLVM_IAS'):
        env.pop(name, None)
    env.update(LC_ALL='C', TZ='UTC', SOURCE_DATE_EPOCH='0', LOCALVERSION='',
               KBUILD_BUILD_USER='builder', KBUILD_BUILD_HOST='github-actions',
               KBUILD_BUILD_VERSION='1', KBUILD_BUILD_TIMESTAMP='1970-01-01 00:00:00')
    return env


def require_lock(workspace):
    import fcntl
    if os.environ.get('OPENWRT_BUILD_WORKSPACE') != str(workspace):
        raise ValueError('Run through tools/build-release.py with this workspace')
    with (workspace / 'locks/openwrt-release.lock').open('rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(lock, fcntl.LOCK_UN)
    raise ValueError('The repository build wrapper must hold the workspace lock')


def owned(info):
    info.uid = info.gid = 0
    info.uname = info.gname = 'root'
    info.mtime = 0
    return info


def perform_build(kernel, workspace, out, jobs):
    if os.name == 'nt':
        raise ValueError('Use the Ubuntu GitHub Actions build environment')
    require_lock(workspace)
    build = workspace / 'objects/tyh212u-platform'
    for left, right in ((kernel, build), (kernel, out), (build, out)):
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError('Source, objects and artifacts must be separate')
    if build.exists() or out.exists():
        raise ValueError('Object or artifact directory already exists; refusing stale inputs')
    if git(kernel, 'rev-parse', 'HEAD').decode().strip() != REVISION:
        raise ValueError('Wrong kernel baseline')
    if git(kernel, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Kernel source must be clean before preparation')
    runpy.run_path(str(HERE / 'prepare-kernel.py'))['apply'](kernel)
    prepared = changed_sources(kernel)
    build.mkdir(parents=True)
    out.mkdir(parents=True)
    env = build_environment()
    seed_path = HERE / 'kernel.config'
    seed = seed_path.read_text(encoding='utf-8')
    make = ['make', '-C', str(kernel), 'O=' + str(build), 'ARCH=arm64',
            'CROSS_COMPILE=' + PREFIX, 'HOSTCC=gcc']
    subprocess.run(make + ['KCONFIG_ALLCONFIG=' + str(seed_path), 'allnoconfig'],
                   env=env, check=True)
    verify_config(seed, (build / '.config').read_text())
    subprocess.run(make + ['-j' + str(jobs), 'Image', 'modules'], env=env, check=True)
    if changed_sources(kernel) != prepared:
        raise ValueError('Prepared kernel sources changed during compilation')
    image = relative_file(build, 'arch/arm64/boot/Image')
    header = image.read_bytes()[:64]
    if len(header) != 64 or header[56:60] != b'ARM\x64':
        raise ValueError('Invalid AArch64 Image header')
    elf = relative_file(build, 'vmlinux').read_bytes()[:20]
    if elf[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', elf, 18)[0] != 183:
        raise ValueError('vmlinux is not little-endian ELF64 AArch64')
    for name in REQUIRED_OBJECTS:
        if relative_file(build, name).stat().st_size == 0:
            raise ValueError('Empty required platform object: ' + name)
    release = relative_file(build, 'include/config/kernel.release').read_text().strip()
    if release != '6.18.54-tyh212u-platform':
        raise ValueError('Unexpected kernel release: ' + release)
    modules = selected_modules(build)
    with tarfile.open(out / 'modules.tar.gz', 'x:gz') as archive:
        for name, source in modules.items():
            archive.add(source, arcname='lib/modules/' + release + '/kernel/' + name,
                        recursive=False, filter=owned)
    with tarfile.open(out / 'prepared-source-overlay.tar.gz', 'x:gz') as archive:
        for name in prepared:
            archive.add(relative_file(kernel, name), arcname=name, recursive=False, filter=owned)
    for name, target in (
            ('arch/arm64/boot/Image', 'Image'), ('.config', 'config'),
            ('System.map', 'System.map'), ('Module.symvers', 'Module.symvers'),
            ('modules.order', 'modules.order'), ('modules.builtin', 'modules.builtin'),
            ('modules.builtin.modinfo', 'modules.builtin.modinfo'),
            ('include/config/kernel.release', 'kernel.release'), ('../COPYING', 'COPYING')):
        source = kernel / 'COPYING' if name == '../COPYING' else relative_file(build, name)
        shutil.copyfile(source, out / target)
    record = {
        'profile': 'tyh212u', 'kernel_revision': REVISION, 'kernel_release': release,
        'kernel_repository': 'https://github.com/Enceka/linux-lts-e5',
        'adapter_revision': git(REPO, 'rev-parse', 'HEAD').decode().strip(),
        'kernel_seed_sha256': digest(seed_path), 'prepared_source_sha256': prepared,
        'required_objects_sha256': {name: digest(build / name) for name in REQUIRED_OBJECTS},
        'module_sha256': {name: digest(path) for name, path in modules.items()},
        'compiler': subprocess.check_output([PREFIX + 'gcc', '--version'], env=env,
                                            text=True).splitlines()[0],
        'boundary': BOUNDARY,
    }
    (out / 'BUILD-RESULT.json').write_text(json.dumps(record, indent=2) + '\n',
                                         encoding='utf-8', newline='\n')
    (out / 'ARTIFACT-BOUNDARY.json').write_text(json.dumps(BOUNDARY, indent=2) + '\n',
                                              encoding='utf-8', newline='\n')
    names = sorted(path.name for path in out.iterdir())
    (out / 'SHA256SUMS').write_text(
        ''.join(digest(out / name) + '  ' + name + '\n' for name in names),
        encoding='utf-8', newline='\n')
    print('Compiled Image and ' + str(len(modules)) + ' matching modules; do not flash.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('kernel', 'workspace', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=2, choices=range(1, 9))
    args = parser.parse_args()
    perform_build(args.kernel.resolve(), args.workspace.resolve(), args.out.resolve(), args.jobs)


if __name__ == '__main__':
    main()
