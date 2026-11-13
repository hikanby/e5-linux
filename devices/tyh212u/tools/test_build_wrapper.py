"""Command-only orchestration boundaries; no firmware, downloads or devices.

Windows exercises main/Task with a lock stand-in and real Python children.
Linux additionally proves that a command child sees the real workspace flock.
"""
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('tyh_build_wrapper', ROOT / 'tools/build-release.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
INSTALLER_ENV = ('E5_BUILD_LAYOUT', 'OPENWRT_GUI_ARTIFACT',
                 'OPENWRT_GUI_MACOS_ARTIFACT', 'OPENWRT_GUI_ANDROID_ARTIFACT')


class PosixWorkspace(PurePosixPath):
    """Exercise the Linux path policy on a Windows test host too."""
    def expanduser(self):
        return self

    def resolve(self):
        return self

    @classmethod
    def home(cls):
        return cls('/home/fixture')


class BuildWrapperTests(unittest.TestCase):
    @contextmanager
    def cli(self, root, arguments):
        # Replace only the wrapper's OS view; do not change pathlib's host OS.
        host = SimpleNamespace(name='posix', environ=os.environ, getpid=os.getpid,
                               open=os.open, fdopen=os.fdopen,
                               O_RDONLY=os.O_RDONLY, O_CREAT=os.O_CREAT)
        with patch.object(build, 'TOP', root), patch.object(build, 'os', host), \
                patch.object(sys, 'argv', ['build-release.py', *arguments]), \
                patch.dict(os.environ, dict(os.environ), clear=True):
            yield

    def test_workspace_policy_is_shared_without_needing_layout(self):
        with patch.object(build, 'Path', PosixWorkspace):
            for path in ('/', '/home/fixture', '/mnt/c', '/mnt/c/build'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    build.validate_workspace(PosixWorkspace(path))
            self.assertEqual(build.validate_workspace(PosixWorkspace('/tmp/tyh212u')),
                             PosixWorkspace('/tmp/tyh212u'))
            self.assertEqual(build.validate_workspace(PosixWorkspace('/mnt/cache')),
                             PosixWorkspace('/mnt/cache'))
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'layout.json'
            config.write_text('{"format": 1}', encoding='utf-8')
            with patch.object(build, 'validate_workspace', side_effect=ValueError('workspace policy')) as check:
                with self.assertRaisesRegex(ValueError, 'workspace policy'):
                    build.layout(config, Path(folder), ['e5'])
                check.assert_called_once()

    def test_all_default_actions_still_require_registered_layout(self):
        actions = ('plan', 'check', 'package', 'refresh-installers',
                   'prepare-installers', 'run', 'status', 'clean-staging')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for action in actions:
                with self.subTest(action=action), self.cli(root, [action]), \
                        patch.object(build, 'layout', side_effect=ValueError('registered layout reached')) as layout:
                    with self.assertRaisesRegex(ValueError, 'registered layout reached'):
                        build.main()
                    self.assertEqual(layout.call_args.args[2], list(build.PROFILES))
            self.assertNotIn('tyh212u', build.PROFILES)
            self.assertNotIn('tyh212u', build.EXPERIMENTAL_PROFILES)

    def test_command_only_is_rejected_for_every_other_action(self):
        actions = ('plan', 'check', 'package', 'refresh-installers',
                   'prepare-installers', 'status', 'clean-staging')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for action in actions:
                with self.subTest(action=action), self.cli(root, [action, '--command-only']), \
                        patch.object(build, 'layout') as layout, \
                        patch.object(build, 'workspace_lock') as lock, \
                        patch.object(sys, 'stderr', io.StringIO()) as error:
                    with self.assertRaises(SystemExit) as stopped:
                        build.main()
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertIn('--command-only is valid only with run', error.getvalue())
                    layout.assert_not_called()
                    lock.assert_not_called()

    def test_default_run_keeps_layout_and_installer_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'native'
            arguments = ['run', '--workspace', str(workspace), '--label', 'default', '--', 'compiler', '--real-flag']
            with self.cli(root, arguments), patch.object(build, 'layout', return_value=({}, {})) as layout, \
                    patch.object(build, 'installer_artifact', side_effect=lambda platform, _workspace: root / platform) as artifacts, \
                    patch.object(build, 'workspace_lock'), patch.object(build, 'Task') as task:
                for key in INSTALLER_ENV:
                    os.environ.pop(key, None)
                build.main()
                layout.assert_called_once_with(root / 'tools/build-layout.json', workspace.resolve(), list(build.PROFILES))
                self.assertEqual(artifacts.call_count, 3)
                self.assertEqual(os.environ['E5_BUILD_LAYOUT'], str((root / 'tools/build-layout.json').resolve()))
                self.assertEqual(os.environ['OPENWRT_GUI_ANDROID_ARTIFACT'], str(root / 'android-aarch64'))
                task.return_value.run.assert_called_once_with('command', ['compiler', '--real-flag'])
                task.return_value.save.assert_called_once_with(state='complete')

    def run_command_only(self, root, command, lock):
        workspace = root / 'native'
        arguments = ['run', '--command-only', '--workspace', str(workspace),
                     '--config', str(root / 'missing-layout.json'), '--label', 'tyh212u-diagnostics', '--', *command]
        with self.cli(root, arguments), patch.object(build, 'workspace_lock', lock), \
                patch.object(build, 'layout', side_effect=AssertionError('unused layout')) as layout, \
                patch.object(build, 'installer_lock', side_effect=AssertionError('unused installer lock')) as pin, \
                patch.object(build, 'installer_artifact', side_effect=AssertionError('unused installer artifact')) as artifact:
            for key in INSTALLER_ENV:
                os.environ.pop(key, None)
            try:
                build.main()
            finally:
                layout.assert_not_called()
                pin.assert_not_called()
                artifact.assert_not_called()
                self.assertTrue(all(key not in os.environ for key in INSTALLER_ENV))
                self.assertEqual(os.environ['OPENWRT_BUILD_WORKSPACE'], str(workspace.resolve()))

    def test_command_only_logs_real_child_and_retains_lock_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = []

            @contextmanager
            def lock(workspace):
                events.append(('enter', workspace))
                try:
                    yield
                finally:
                    events.append(('exit', workspace))

            run_child = subprocess.run

            def checked_child(command, **kwargs):
                self.assertEqual([event for event, _ in events], ['enter'])
                self.assertEqual(kwargs['cwd'], root)
                self.assertNotIn('shell', kwargs)
                self.assertTrue(Path(kwargs['env']['TMPDIR']).is_relative_to(root / 'native/tmp'))
                return run_child(command, **kwargs)

            command = [sys.executable, '-c', 'import os; print("diagnostic-log-output"); print(os.getcwd())']
            with patch.object(build.subprocess, 'run', side_effect=checked_child):
                self.run_command_only(root, command, lock)
            self.assertEqual([event for event, _ in events], ['enter', 'exit'])
            status = json.loads((root / 'out/build-state/latest.json').read_text())
            self.assertEqual(status['state'], 'complete')
            self.assertEqual(status['steps'][0]['command'], command)
            self.assertEqual(status['steps'][0]['state'], 'complete')
            log = Path(status['steps'][0]['log'])
            self.assertIn('diagnostic-log-output', log.read_text())
            self.assertEqual(json.loads((log.parent / 'status.json').read_text()), status)
            self.assertEqual(list((root / 'native/tmp').iterdir()), [])
            self.assertFalse((root / 'out/releases').exists())

    def test_command_only_failure_records_failed_status_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = []

            @contextmanager
            def lock(_workspace):
                events.append('enter')
                try:
                    yield
                finally:
                    events.append('exit')

            command = [sys.executable, '-c', 'print("failure-log-output"); raise SystemExit(7)']
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_command_only(root, command, lock)
            status = json.loads((root / 'out/build-state/latest.json').read_text())
            self.assertEqual(status['state'], 'failed')
            self.assertEqual(status['steps'][0]['state'], 'failed')
            self.assertIn('failure-log-output', Path(status['steps'][0]['log']).read_text())
            self.assertIn('7', status['error'])
            self.assertEqual(events, ['enter', 'exit'])
            self.assertEqual(list((root / 'native/tmp').iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'requires Linux for the real flock integration test')
    def test_command_only_child_sees_real_shared_workspace_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = [sys.executable, '-c',
                       'import fcntl,sys; f=open(sys.argv[1], "rb"); '
                       '\ntry: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)'
                       '\nexcept BlockingIOError: print("shared-lock-held")'
                       '\nelse: raise SystemExit("workspace lock was not held")',
                       str(root / 'native/locks/openwrt-release.lock')]
            self.run_command_only(root, command, build.workspace_lock)
            status = json.loads((root / 'out/build-state/latest.json').read_text())
            self.assertIn('shared-lock-held', Path(status['steps'][0]['log']).read_text())
            with build.workspace_lock(root / 'native'):
                pass  # The parent released its lock after the command finished.


if __name__ == '__main__':
    unittest.main()
