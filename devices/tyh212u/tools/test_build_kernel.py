"""Exercise build input and artifact validation without compiling a kernel."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'tyh_kernel_builder', Path(__file__).resolve().parents[1] / 'build-kernel.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class KernelBuildValidationTests(unittest.TestCase):
    def test_required_configuration_cannot_be_dropped_or_changed(self):
        seed = 'CONFIG_BLOCK=y\nCONFIG_TUN=m\nCONFIG_LOCALVERSION="-test"\n'
        builder.verify_config(seed, seed + '# CONFIG_DEBUG is not set\n')
        for actual in (seed.replace('CONFIG_BLOCK=y\n', ''),
                       seed.replace('CONFIG_TUN=m', 'CONFIG_TUN=y'),
                       seed.replace('"-test"', '"-other"')):
            with self.assertRaisesRegex(ValueError, 'dropped required settings'):
                builder.verify_config(seed, actual)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            builder.verify_config(seed, seed + 'CONFIG_BLOCK=n\n')

    def test_modules_order_requires_complete_linked_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / 'drivers/net/tun'
            module.parent.mkdir(parents=True)
            module.with_suffix('.o').write_bytes(b'object')
            (root / 'modules.order').write_text('drivers/net/tun.o\n')
            with self.assertRaisesRegex(ValueError, 'Missing build input'):
                builder.selected_modules(root)
            module.with_suffix('.ko').write_bytes(b'linked module')
            self.assertEqual(builder.selected_modules(root),
                             {'drivers/net/tun.ko': module.with_suffix('.ko')})
            for text in ('', 'drivers/net/tun.ko\n',
                         'drivers/net/tun.o\ndrivers/net/tun.o\n',
                         '../outside.o\n', '/outside.o\n'):
                (root / 'modules.order').write_text(text)
                with self.assertRaises(ValueError):
                    builder.selected_modules(root)

    def test_paths_cannot_escape_artifact_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'objects'
            root.mkdir()
            (root / 'valid').write_bytes(b'ok')
            self.assertEqual(builder.relative_file(root, 'valid'), root / 'valid')
            for name in ('../outside', '/outside', 'a\\b', ''):
                with self.assertRaises(ValueError):
                    builder.relative_file(root, name)

    def test_source_overlay_includes_tracked_and_new_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = ['git', '-c', 'safe.directory=' + str(root),
                   '-c', 'user.name=Kernel Test',
                   '-c', 'user.email=kernel@example.invalid',
                   '-c', 'commit.gpgsign=false', '-C', str(root)]
            subprocess.run(git + ['init', '--quiet', '--template='], check=True)
            (root / 'Kconfig').write_bytes(b'baseline\n')
            subprocess.run(git + ['add', 'Kconfig'], check=True)
            subprocess.run(git + ['commit', '--quiet', '-m', 'baseline'], check=True)
            (root / 'Kconfig').write_bytes(b'changed\n')
            (root / 'new.c').write_bytes(b'new driver\n')
            self.assertEqual(builder.changed_sources(root), {
                name: hashlib.sha256(data).hexdigest() for name, data in
                (('Kconfig', b'changed\n'), ('new.c', b'new driver\n'))})

    def test_environment_removes_inherited_build_overrides(self):
        with patch.dict(os.environ, {'CC': 'other-cc', 'KCFLAGS': '-bad',
                                    'KBUILD_OUTPUT': '/wrong', 'KCONFIG_CONFIG': '/wrong'}):
            env = builder.build_environment()
        for name in ('CC', 'KCFLAGS', 'KBUILD_OUTPUT', 'KCONFIG_CONFIG'):
            self.assertNotIn(name, env)
        self.assertEqual(env['LOCALVERSION'], '')

    @unittest.skipIf(os.name == 'nt', 'Linux flock semantics')
    def test_build_requires_a_lock_held_by_wrapper(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'locks').mkdir()
            path = root / 'locks/openwrt-release.lock'
            path.touch()
            with patch.dict(os.environ, {'OPENWRT_BUILD_WORKSPACE': str(root)}):
                with self.assertRaisesRegex(ValueError, 'must hold'):
                    builder.require_lock(root)
                with path.open('rb') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    builder.require_lock(root)


if __name__ == '__main__':
    unittest.main()
