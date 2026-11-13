#!/usr/bin/env python3
"""Build only a TYH212U diagnostic RAM archive on Ubuntu 24.04 x86_64.

Invoke through tools/build-release.py run for the repository workspace lock
and task logs. This entry point never builds a kernel, boot image, installation
package or production release, and never contacts a phone.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCK_PATH = HERE / "diagnostics-busybox.lock.json"
SEED_PATH = HERE / "diagnostics-busybox.config"
JOBS = 2
PREFIX = "aarch64-linux-gnu-"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
ALLOWED_APPLETS = frozenset({"[", "ash", "busybox", "cat", "mount", "printf", "readlink", "sh", "sleep", "uname"})
TOOLS = ("make", "gcc", PREFIX + "gcc", PREFIX + "ld", PREFIX + "strip", PREFIX + "readelf",
         "qemu-aarch64", "file", "git")
BOUNDARY = {
    "artifact_kind": "diagnostic-initramfs-only",
    "model": "TYH212U / ARES",
    "kernel_built": False,
    "boot_image_built": False,
    "openwrt_firmware_built": False,
    "avb_signed_or_verified": False,
    "phone_contacted": False,
    "phone_boot_or_hardware_validated": False,
    "init_executed": False,
    "deployment_authorized_by_this_artifact": False,
    "instructions": "Do not flash this archive. It is not a boot image or a usable OpenWrt release."
}


class BuildError(ValueError):
    pass


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def workspace_child(workspace: Path, relative: str) -> Path:
    target = workspace / relative
    if not target.resolve().is_relative_to(workspace.resolve()):
        raise BuildError("Fixed build path escaped the explicit workspace")
    for path in (target, *target.parents):
        if path == workspace:
            break
        if path.is_symlink():
            raise BuildError("Symlinked build cache path is not accepted: " + str(path))
    return target


def load_lock(path: Path = LOCK_PATH) -> dict:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or lock.get("schema") != 1 or lock.get("name") != "busybox":
        raise BuildError("Invalid BusyBox lock schema/name")
    version = lock.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BuildError("Invalid BusyBox version")
    filename = f"busybox-{version}.tar.bz2"
    if lock.get("source_url") != "https://busybox.net/downloads/" + filename:
        raise BuildError("Only the exact locked official BusyBox release URL is accepted")
    if not isinstance(lock.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", lock["sha256"]):
        raise BuildError("Missing exact source SHA-256")
    if lock.get("official_checksum_url") != lock["source_url"] + ".sha256":
        raise BuildError("Invalid official checksum source")
    if lock.get("official_checksum_line") != lock["sha256"] + "  " + filename:
        raise BuildError("Lock does not preserve the published checksum line")
    return lock


def config_values(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"CONFIG_([A-Z0-9_]+)=(.+)", line)
        disabled = re.fullmatch(r"# CONFIG_([A-Z0-9_]+) is not set", line)
        if match or disabled:
            key, value = (match[1], match[2]) if match else (disabled[1], "n")
            if key in result:
                raise BuildError("Duplicate config key: " + key)
            result[key] = value
    return result


def verify_config(text: str, seed: str) -> None:
    actual, expected = config_values(text), config_values(seed)
    for name, value in expected.items():
        if actual.get(name, "n") != value:
            raise BuildError(f"BusyBox resolved CONFIG_{name}={actual.get(name, 'n')}, expected {value}")
    for name in ("STATIC", "SH_IS_ASH", "CAT", "PRINTF", "READLINK", "SLEEP", "UNAME", "TEST1", "MOUNT", "FEATURE_MOUNT_FLAGS"):
        if actual.get(name) != "y":
            raise BuildError("Missing essential config: " + name)
    for name in ("PIE", "BUILD_LIBBUSYBOX", "FEATURE_MOUNT_FSTAB", "FEATURE_MOUNT_HELPERS", "FEATURE_MOUNT_LABEL", "FEATURE_MTAB_SUPPORT"):
        if actual.get(name, "n") != "n":
            raise BuildError("Unexpected enabled config: " + name)


def verify_applets(text: str, required: frozenset[str]) -> list[str]:
    applets = text.splitlines()
    if not applets or any(not a or a.strip() != a or "/" in a for a in applets) or len(set(applets)) != len(applets):
        raise BuildError("Invalid qemu BusyBox --list output")
    present = set(applets)
    if not required <= present:
        raise BuildError("Built BusyBox lacks applets: " + ", ".join(sorted(required - present)))
    if present - ALLOWED_APPLETS:
        raise BuildError("Unexpected applets in minimal build: " + ", ".join(sorted(present - ALLOWED_APPLETS)))
    return sorted(applets)


def inspect_tar_members(members: list[tarfile.TarInfo], prefix: str) -> None:
    if not 1 <= len(members) <= 10000 or sum(m.size for m in members) > MAX_SOURCE_BYTES:
        raise BuildError("Source tar archive exceeds expected size/member limits")
    names = set()
    for member in members:
        name = member.name.rstrip("/") if member.isdir() else member.name
        path = PurePosixPath(name)
        if (not name or name.startswith("/") or "\\" in name or any(x in ("", ".", "..") for x in name.split("/"))
                or path.parts[0] != prefix or name in names or not (member.isdir() or member.isfile())
                or member.size < 0 or member.size > 16 * 1024 * 1024):
            raise BuildError("Unsafe or unsupported source archive member: " + member.name)
        names.add(name)


def download_locked(lock: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"busybox-{lock['version']}.tar.bz2"
    if target.exists():
        if target.is_symlink() or not target.is_file() or digest_file(target) != lock["sha256"]:
            raise BuildError("Existing download fails identity check; retained for inspection")
        return target
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(lock["source_url"], headers={"User-Agent": "TYH212U-diagnostic-build/1"})
    with partial.open("xb") as output, urllib.request.urlopen(request, timeout=60) as source:
        total = 0
        while block := source.read(1024 * 1024):
            total += len(block)
            if total > 16 * 1024 * 1024:
                raise BuildError("Source download exceeds size limit")
            output.write(block)
    if digest_file(partial) != lock["sha256"]:
        raise BuildError("Official source SHA-256 mismatch; partial retained")
    # Same-filesystem hard link gives exclusive creation rather than overwriting
    # a target another process may have created between the initial check/write.
    os.link(partial, target)
    partial.unlink()
    return target


def extract_fresh(archive: Path, target: Path, prefix: str, identity: dict) -> None:
    if target.exists() or target.is_symlink():
        raise BuildError("Prepared source path already exists; no implicit stale-tree reuse: " + str(target))
    with tarfile.open(archive, mode="r:bz2") as source:
        members = source.getmembers()
        inspect_tar_members(members, prefix)
        target.mkdir(parents=True, exist_ok=False)
        for member in members:
            parts = PurePosixPath(member.name.rstrip("/")).parts[1:]
            destination = target.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(0o755)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = source.extractfile(member)
                if stream is None:
                    raise BuildError("Unable to read regular source file")
                with stream, destination.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                destination.chmod(member.mode & 0o777)
            # All members were checked to exclude links before extraction.
            # Linux supports the explicit no-follow form; the fallback allows
            # offline extraction tests on Windows without running a build.
            timestamp_options = {"follow_symlinks": False} if os.utime in os.supports_follow_symlinks else {}
            os.utime(destination, (0, 0), **timestamp_options)
    write_json_new(target / ".tyh212u-diagnostics-source.json", identity)


def write_json_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=2, ensure_ascii=False, sort_keys=True)
        output.write("\n")


def copy_new(source: Path, target: Path) -> None:
    with source.open("rb") as input_file, target.open("xb") as output:
        shutil.copyfileobj(input_file, output)
    if digest_file(source) != digest_file(target):
        raise BuildError("Artifact copy hash mismatch")


def run(command: list[str], *, env: dict, cwd: Path | None = None, capture=True, data: bytes | None = None,
        timeout=120) -> subprocess.CompletedProcess:
    print("+ " + shlex.join(map(str, command)), flush=True)
    return subprocess.run(command, cwd=cwd, env=env, input=data, check=True, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)


def load_archive_tool():
    path = HERE / "tools/diagnostic_initramfs.py"
    spec = importlib.util.spec_from_file_location("tyh212u_diagnostic_initramfs", path)
    if spec is None or spec.loader is None:
        raise BuildError("Cannot load tracked diagnostic archive implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def input_identity() -> dict[str, str]:
    names = ("build-diagnostics.py", "diagnostics-busybox.lock.json", "diagnostics-busybox.config",
             "tools/diagnostic_initramfs.py")
    return {name: digest_file(HERE / name) for name in names}


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("CFLAGS", "CPPFLAGS", "LDFLAGS", "LDLIBS", "MAKEFLAGS", "MFLAGS", "KBUILD_SRC", "KBUILD_OUTPUT",
                 "KCONFIG_CONFIG", "KCONFIG_ALLCONFIG", "CROSS_COMPILE", "CC", "HOSTCC", "ARCH"):
        env.pop(name, None)
    env.update({"LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": "0", "KCONFIG_NOTIMESTAMP": "1",
                "KBUILD_BUILD_USER": "builder", "KBUILD_BUILD_HOST": "github-actions",
                "KBUILD_BUILD_TIMESTAMP": "1970-01-01 00:00:00"})
    return env


def preflight(workspace: Path, out: Path) -> None:
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise BuildError("This tracked build entry point requires Linux x86_64; use the Ubuntu 24.04 CI workflow")
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if not re.search(r'^ID=ubuntu$', os_release, re.M) or not re.search(r'^VERSION_ID="24\.04"$', os_release, re.M):
        raise BuildError("First supported build host is Ubuntu 24.04")
    missing = [name for name in TOOLS if not shutil.which(name)]
    if missing:
        raise BuildError("Missing declared build tools: " + ", ".join(missing))
    if (not workspace.is_absolute() or not out.is_absolute() or workspace in (Path("/"), Path.home())
            or out in (Path("/"), Path.home(), workspace, REPO) or workspace.is_relative_to("/mnt/c")
            or any(c.isspace() for c in str(workspace) + str(out))):
        raise BuildError("Use explicit dedicated absolute workspace/output paths without whitespace; no C: build migration")
    if out.exists() or out.is_symlink():
        raise BuildError("Output directory already exists; refusing overwrite/reuse: " + str(out))
    for subtree in ("sources", "objects", "downloads"):
        if out.is_relative_to(workspace / subtree):
            raise BuildError("Artifact output cannot replace a source/object/download tree")


def smoke_busybox(binary: Path, init_path: Path, scratch: Path, required: frozenset[str], env: dict) -> tuple[list[str], list[dict]]:
    qemu = ["qemu-aarch64", str(binary)]
    listing = run(qemu + ["--list"], env=env).stdout.decode("utf-8")
    applets = verify_applets(listing, required)
    scratch.mkdir(exist_ok=False)
    sample = scratch / "sample.txt"
    sample.write_bytes(b"TYH212U harmless applet smoke\n")
    link = scratch / "sample-link"
    link.symlink_to("sample.txt")
    results = []

    def check(label, arguments, expected=None):
        completed = run(qemu + arguments, env=env, timeout=30)
        if expected is not None and completed.stdout != expected:
            raise BuildError(label + " smoke returned unexpected stdout")
        results.append({"check": label, "passed": True})
        return completed

    check("printf", ["printf", "%s\n", "diagnostic-smoke"], b"diagnostic-smoke\n")
    check("cat", ["cat", str(sample)], sample.read_bytes())
    check("readlink", ["readlink", str(link)], b"sample.txt\n")
    check("test-bracket", ["[", "-r", str(sample), "]"], b"")
    check("sleep-zero", ["sleep", "0"], b"")
    check("uname-machine", ["uname", "-m"], b"aarch64\n")
    help_result = check("mount-help-only", ["mount", "--help"])
    help_text = help_result.stdout + help_result.stderr
    if b"mount" not in help_text or b"-o" not in help_text:
        raise BuildError("Minimal mount help does not show explicit options support")
    check("ash-builtins-only", ["sh", "-c", 'trap "" HUP INT TERM; [ -r "$1" ] || exit 9; printf "%s\\n" "${1##*/}"',
                                "diagnostic-smoke", str(sample)], b"sample.txt\n")
    # -n parses only. Never invoke this file as a command or without -n.
    check("init-syntax-only-not-executed", ["sh", "-n", str(init_path)], b"")
    return applets, results


def perform_build(workspace: Path, out: Path, lock: dict, env: dict) -> dict:
    archive_tool = load_archive_tool()
    initial_inputs = input_identity()
    source = workspace_child(workspace, f"sources/tyh212u-diagnostics/busybox-{lock['version']}")
    objects = workspace_child(workspace, f"objects/tyh212u-diagnostics/busybox-{lock['version']}")
    if source.exists() or objects.exists() or source.is_symlink() or objects.is_symlink():
        raise BuildError("Fixed source/object tree already exists; no unreviewed prepared-cache reuse")
    cached = download_locked(lock, workspace_child(workspace, "downloads/tyh212u-diagnostics"))
    identity = {"source_sha256": lock["sha256"], "seed_sha256": initial_inputs["diagnostics-busybox.config"],
                "cross_compile": PREFIX, "jobs": JOBS}
    extract_fresh(cached, source, "busybox-" + lock["version"], identity)
    objects.mkdir(parents=True, exist_ok=False)
    write_json_new(objects / ".tyh212u-diagnostics-objects.json", identity)
    make = ["make", "-C", str(source), "O=" + str(objects), "CROSS_COMPILE=" + PREFIX, "HOSTCC=gcc"]
    configure_env = dict(env, KCONFIG_ALLCONFIG=str(SEED_PATH))
    run(make + ["allnoconfig"], env=configure_env, capture=False, timeout=600)
    resolved_config = (objects / ".config").read_text(encoding="utf-8")
    verify_config(resolved_config, SEED_PATH.read_text(encoding="utf-8"))
    run(make + [f"-j{JOBS}", "busybox"], env=env, capture=False, timeout=1200)
    binary_path = objects / "busybox"
    binary = binary_path.read_bytes()
    elf = archive_tool.validate_busybox_elf(binary)
    if run([PREFIX + "gcc", "-dumpmachine"], env=env).stdout.strip() != b"aarch64-linux-gnu":
        raise BuildError("Cross compiler target does not match the declared target")
    copy_new(binary_path, out / "busybox.aarch64")
    (out / "busybox.aarch64").chmod(0o755)
    # Bind the real compiled applet list first, then syntax-check only the init
    # extracted from the actual generated archive, never a parallel template.
    listing = run(["qemu-aarch64", str(binary_path), "--list"], env=env).stdout.decode("utf-8")
    applets = verify_applets(listing, archive_tool.REQUIRED_APPLETS)
    manifest = {"schema": 1, "sha256": elf["sha256"], "applets": applets,
                "method": "qemu-aarch64 built-busybox --list; required applets smoke-tested",
                "busybox_version": lock["version"]}
    compressed = archive_tool.build(binary, manifest)
    recovered = archive_tool.parse_newc(gzip.decompress(compressed))
    extracted_init = dict((name, data) for name, _mode, data, _rdev in recovered).get("init")
    if not isinstance(extracted_init, bytes) or extracted_init != archive_tool.INIT:
        raise BuildError("Generated archive does not contain the tracked diagnostic init")
    with (out / "diagnostic-init.sh").open("xb") as init_output:
        init_output.write(extracted_init)
    checked_applets, smoke = smoke_busybox(binary_path, out / "diagnostic-init.sh", objects / "smoke", archive_tool.REQUIRED_APPLETS, env)
    if checked_applets != applets:
        raise BuildError("Built applet list changed between archive generation and smoke tests")
    write_json_new(out / "busybox-applets.json", manifest)
    if extracted_init != (out / "diagnostic-init.sh").read_bytes():
        raise BuildError("Validated init differs from the final initramfs")
    archive_tool.write_new(out / "diagnostic.cpio.gz", compressed)
    copy_new(objects / ".config", out / "busybox.config")
    copy_new(SEED_PATH, out / "busybox-seed.config")
    copy_new(LOCK_PATH, out / "busybox-source.lock.json")
    copy_new(cached, out / cached.name)
    copy_new(source / "LICENSE", out / "LICENSE.busybox")
    file_evidence = run(["file", str(binary_path)], env=env).stdout.decode("utf-8", errors="replace")
    readelf_evidence = run([PREFIX + "readelf", "-h", "-l", str(binary_path)], env=env).stdout.decode("utf-8", errors="replace")
    with (out / "busybox-elf.txt").open("x", encoding="utf-8", newline="\n") as evidence:
        evidence.write(file_evidence + "\n" + readelf_evidence)
    versions = {}
    for tool in ("gcc", PREFIX + "gcc", PREFIX + "ld", "make", "qemu-aarch64", "file"):
        result = run([tool, "--version"], env=env)
        versions[tool] = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
    commit = run(["git", "-C", str(REPO), "rev-parse", "HEAD"], env=env).stdout.decode().strip()
    if input_identity() != initial_inputs:
        raise BuildError("Tracked diagnostic build inputs changed during the build")
    return {"schema": 1, "repository_commit": commit, "source": lock, "input_sha256": initial_inputs,
            "busybox_elf": elf, "toolchain_versions": versions, "build_jobs": JOBS,
            "config_method": "BusyBox allnoconfig with tracked KCONFIG_ALLCONFIG seed",
            "checks": smoke, "archive_entries": [name for name, _mode, _data, _rdev in recovered],
            "boundary": BOUNDARY, "source_date_epoch": 0,
            "reproducible_binary_across_different_toolchains_claimed": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True, help="Explicit dedicated Linux workspace (same as build-release.py run)")
    parser.add_argument("--out", type=Path, required=True, help="New absolute artifact directory; existing outputs are refused")
    args = parser.parse_args(argv)
    workspace, out = args.workspace.expanduser(), args.out.expanduser()
    owned_out = False
    try:
        preflight(workspace, out)
        # Resolve only after rejecting user-facing relative paths.
        workspace, out = workspace.resolve(), out.resolve()
        preflight(workspace, out)
        lock = load_lock()
        out.mkdir(parents=True, exist_ok=False)
        owned_out = True
        write_json_new(out / "ARTIFACT-BOUNDARY.json", BOUNDARY)
        provenance = perform_build(workspace, out, lock, build_environment())
        provenance["built_at_utc"] = datetime.now(timezone.utc).isoformat()
        provenance["files"] = {p.name: {"sha256": digest_file(p), "bytes": p.stat().st_size}
                               for p in sorted(out.iterdir()) if p.is_file()}
        write_json_new(out / "diagnostics-provenance.json", provenance)
        with (out / "SHA256SUMS").open("x", encoding="ascii", newline="\n") as checksums:
            for path in sorted(out.iterdir()):
                if path.is_file() and path.name != "SHA256SUMS":
                    checksums.write(digest_file(path) + "  " + path.name + "\n")
        print(json.dumps({"success": True, "out": str(out), "boundary": BOUNDARY}, ensure_ascii=False))
        return 0
    except (BuildError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        print("Diagnostic build failed: " + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        if owned_out:
            write_json_new(out / "DIAGNOSTICS-FAILED.json", {"failed": True, "error": str(error), "boundary": BOUNDARY})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
