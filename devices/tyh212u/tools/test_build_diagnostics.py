"""Offline logic tests only; these do not claim a successful Linux compilation."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

MODEL = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tyh212u_build_diagnostics_tests", MODEL / "build-diagnostics.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildDiagnosticsTests(unittest.TestCase):
    def test_published_source_lock_and_seed(self):
        lock = builder.load_lock()
        self.assertEqual(lock["version"], "1.37.0")
        self.assertEqual(lock["sha256"], "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4")
        seed = builder.SEED_PATH.read_text()
        builder.verify_config(seed, seed)
        self.assertEqual(builder.JOBS, 2)

    def test_lock_rejects_unpinned_or_different_source(self):
        lock = builder.load_lock()
        for key, value in (("sha256", "unknown"), ("source_url", "https://example.com/source.tar.bz2"),
                           ("official_checksum_line", "bad"), ("version", "latest")):
            changed = dict(lock, **{key: value})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "lock.json"
                path.write_text(json.dumps(changed))
                with self.subTest(key=key), self.assertRaises(builder.BuildError):
                    builder.load_lock(path)

    def test_resolved_config_must_keep_mount_flags_static_and_no_fstab(self):
        seed = builder.SEED_PATH.read_text()
        for before, after in (("CONFIG_STATIC=y", "# CONFIG_STATIC is not set"),
                              ("CONFIG_FEATURE_MOUNT_FLAGS=y", "# CONFIG_FEATURE_MOUNT_FLAGS is not set"),
                              ("# CONFIG_FEATURE_MOUNT_FSTAB is not set", "CONFIG_FEATURE_MOUNT_FSTAB=y")):
            with self.subTest(before=before), self.assertRaises(builder.BuildError):
                builder.verify_config(seed.replace(before, after), seed)
        with self.assertRaises(builder.BuildError):
            builder.config_values("CONFIG_STATIC=y\nCONFIG_STATIC=y\n")

    def test_actual_applet_list_required_and_unexpected_rejected(self):
        required = builder.load_archive_tool().REQUIRED_APPLETS
        listing = "\n".join(sorted(required | {"ash"})) + "\n"
        self.assertEqual(builder.verify_applets(listing, required), sorted(required | {"ash"}))
        for invalid in (listing + "dd\n", listing.replace("mount\n", ""), listing + "sh\n", listing + "bad/name\n"):
            with self.assertRaises(builder.BuildError):
                builder.verify_applets(invalid, required)

    def test_tar_members_reject_traversal_links_duplicates_and_special(self):
        good = tarfile.TarInfo("busybox-1.37.0/Makefile")
        good.size = 1
        builder.inspect_tar_members([good], "busybox-1.37.0")
        for name, typ in (("busybox-1.37.0/../escape", tarfile.REGTYPE),
                          ("/busybox-1.37.0/a", tarfile.REGTYPE),
                          ("other/a", tarfile.REGTYPE),
                          ("busybox-1.37.0/link", tarfile.SYMTYPE),
                          ("busybox-1.37.0/device", tarfile.CHRTYPE)):
            member = tarfile.TarInfo(name)
            member.type = typ
            with self.subTest(name=name), self.assertRaises(builder.BuildError):
                builder.inspect_tar_members([member], "busybox-1.37.0")
        with self.assertRaises(builder.BuildError):
            builder.inspect_tar_members([good, copy.copy(good)], "busybox-1.37.0")

    def test_extract_only_fresh_source_and_preserve_failed_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = directory / "source.tar.bz2"
            with tarfile.open(archive, "w:bz2") as target:
                item = tarfile.TarInfo("busybox-1.37.0/Makefile")
                item.size = 4
                item.mode = 0o644
                target.addfile(item, io.BytesIO(b"test"))
            source = directory / "prepared"
            builder.extract_fresh(archive, source, "busybox-1.37.0", {"test": True})
            self.assertEqual((source / "Makefile").read_bytes(), b"test")
            with self.assertRaises(builder.BuildError):
                builder.extract_fresh(archive, source, "busybox-1.37.0", {})
            self.assertEqual((source / "Makefile").read_bytes(), b"test")

    def test_download_cache_identity_is_checked_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            target = directory / "busybox-1.37.0.tar.bz2"
            target.write_bytes(b"cached")
            lock = {"version": "1.37.0", "sha256": hashlib.sha256(b"cached").hexdigest()}
            with mock.patch.object(builder.urllib.request, "urlopen", side_effect=AssertionError("network not expected")):
                self.assertEqual(builder.download_locked(lock, directory), target)
            target.write_bytes(b"changed")
            with self.assertRaises(builder.BuildError):
                builder.download_locked(lock, directory)
            self.assertEqual(target.read_bytes(), b"changed")

    def test_bad_download_retains_partial_and_never_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            lock = {"version": "1.37.0", "sha256": "0" * 64,
                    "source_url": "https://busybox.net/downloads/busybox-1.37.0.tar.bz2"}
            with mock.patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaises(builder.BuildError):
                    builder.download_locked(lock, directory)
            self.assertFalse((directory / "busybox-1.37.0.tar.bz2").exists())
            self.assertEqual((directory / "busybox-1.37.0.tar.bz2.partial").read_bytes(), b"wrong")

    def test_copy_and_json_outputs_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, target = directory / "source", directory / "target"
            source.write_bytes(b"source")
            target.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                builder.copy_new(source, target)
            with self.assertRaises(FileExistsError):
                builder.write_json_new(target, {})
            self.assertEqual(target.read_bytes(), b"original")

    def test_environment_cannot_inherit_foreign_make_config_or_compiler(self):
        with mock.patch.dict(builder.os.environ, {"CFLAGS": "bad", "KBUILD_OUTPUT": "bad", "CROSS_COMPILE": "bad", "MAKEFLAGS": "bad"}):
            env = builder.build_environment()
        for key in ("CFLAGS", "KBUILD_OUTPUT", "CROSS_COMPILE", "MAKEFLAGS"):
            self.assertNotIn(key, env)
        self.assertEqual(env["SOURCE_DATE_EPOCH"], "0")

    def test_workspace_child_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.assertEqual(builder.workspace_child(workspace, "sources/tyh212u"), workspace / "sources/tyh212u")
            with self.assertRaises(builder.BuildError):
                builder.workspace_child(workspace, "../escaped")

    def test_qemu_smoke_never_executes_init_or_mount(self):
        archive_tool = builder.load_archive_tool()
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            init = directory / "init.sh"
            init.write_bytes(archive_tool.INIT)

            def fake_run(command, **kwargs):
                commands.append(command)
                args = command[2:]
                if args == ["--list"]:
                    stdout = ("\n".join(sorted(archive_tool.REQUIRED_APPLETS | {"ash"})) + "\n").encode()
                elif args[0] == "printf": stdout = b"diagnostic-smoke\n"
                elif args[0] == "cat": stdout = b"TYH212U harmless applet smoke\n"
                elif args[0] == "readlink": stdout = b"sample.txt\n"
                elif args == ["uname", "-m"]: stdout = b"aarch64\n"
                elif args == ["mount", "--help"]: stdout = b"mount [-o OPT] [-t TYPE] DEVICE DIR\n"
                elif args[:2] == ["sh", "-c"]: stdout = b"sample.txt\n"
                else: stdout = b""
                return builder.subprocess.CompletedProcess(command, 0, stdout, b"")

            with mock.patch.object(Path, "symlink_to"), mock.patch.object(builder, "run", side_effect=fake_run):
                applets, results = builder.smoke_busybox(directory / "busybox", init, directory / "smoke", archive_tool.REQUIRED_APPLETS, {})
            self.assertTrue(archive_tool.REQUIRED_APPLETS <= set(applets))
            self.assertEqual(len(results), 9)
            init_commands = [cmd for cmd in commands if str(init) in cmd]
            self.assertEqual(init_commands, [["qemu-aarch64", str(directory / "busybox"), "sh", "-n", str(init)]])
            self.assertEqual([cmd[2:] for cmd in commands if cmd[2:3] == ["mount"]], [["mount", "--help"]])


if __name__ == "__main__":
    unittest.main()
