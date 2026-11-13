"""Offline source provenance/staging tests, not USB hardware validation."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

DEVICE = Path(__file__).resolve().parents[1]
PORT = runpy.run_path(str(DEVICE / 'usb-phy.py'))
APPLY = PORT['apply']
MANIFEST = json.loads(PORT['MANIFEST'].read_text(encoding='utf-8'))


class UsbPhyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'kernel'
        self.root.mkdir()
        self.manifest = copy.deepcopy(MANIFEST)
        for name in PORT['API_FILES']:
            data = ('/* compact API fixture: ' + name + ' */\n').encode()
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.manifest['kernel_api_files'][name] = hashlib.sha256(data).hexdigest()
        directory = self.root / 'drivers/usb/phy'
        directory.mkdir(parents=True)
        (directory / 'Kconfig').write_bytes(b'# baseline\nendmenu\n')
        (directory / 'Makefile').write_bytes(b'# baseline\nobj-y += unrelated.o\n')
        self.manifest_path = Path(self.temp.name) / 'manifest.json'
        self.save_manifest()
        patched = patch.dict(APPLY.__globals__, {'MANIFEST': self.manifest_path})
        patched.start()
        self.addCleanup(patched.stop)

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_install_is_idempotent_and_matches_real_source_and_output_digests(self):
        original = self.snapshot()
        APPLY(self.root)
        first = self.snapshot()
        APPLY(self.root)
        self.assertEqual(first, self.snapshot())
        for name, expected in MANIFEST['files'].items():
            data = (PORT['SOURCE_ROOT'] / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected, name)
        for name, expected in MANIFEST['prepared_files'].items():
            data = (self.root / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected, name)
            self.assertNotIn(b'\r\n', data)
        for name in PORT['API_FILES']:
            self.assertEqual(first[name], original[name])

    def test_each_changed_api_file_is_rejected_without_writes(self):
        for name in PORT['API_FILES']:
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b'wrong API\n')
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'pinned USB PHY kernel API'):
                    APPLY(self.root)
                self.assertEqual(before, self.snapshot())
                path.write_bytes(original)

    def test_foreign_driver_and_header_are_rejected_without_writes(self):
        for name in (PORT['DRIVER'], PORT['FILES'][1]):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'other port\n')
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'Foreign USB PHY destination'):
                    APPLY(self.root)
                self.assertEqual(before, self.snapshot())
                path.unlink()

    def test_conflicting_build_entries_are_rejected_before_source_install(self):
        for name, data in (
            ('Kconfig', b'config USB_SPRD_UMS9620_SSPHY\n\tbool "Other"\n'),
            ('Makefile', b'obj-$(CONFIG_OTHER) += phy-sprd-ums9620-ssphy.o\n'),
        ):
            path = self.root / 'drivers/usb/phy' / name
            original = path.read_bytes()
            path.write_bytes(original + data)
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, 'Conflicting USB PHY'):
                APPLY(self.root)
            self.assertEqual(before, self.snapshot())
            path.write_bytes(original)

    def test_duplicate_prepared_entry_is_rejected(self):
        APPLY(self.root)
        path = self.root / 'drivers/usb/phy/Makefile'
        path.write_bytes(path.read_bytes() + PORT['MAKEFILE'].encode())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting USB PHY'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_tampered_donor_and_changed_prepared_digest_are_rejected(self):
        sources = Path(self.temp.name) / 'source'
        for name in PORT['FILES']:
            target = sources / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PORT['SOURCE_ROOT'] / name, target)
        path = sources / PORT['DRIVER']
        path.write_bytes(path.read_bytes() + b'/* changed */\n')
        before = self.snapshot()
        with patch.dict(APPLY.__globals__, {'SOURCE_ROOT': sources}):
            with self.assertRaisesRegex(ValueError, 'donor hash mismatch'):
                APPLY(self.root)
        self.assertEqual(before, self.snapshot())
        self.manifest['prepared_files'][PORT['DRIVER']] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'prepared hash mismatch'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_manifest_cannot_redirect_reads_outside_declared_files(self):
        self.manifest['kernel_api_files']['../outside'] = '0' * 64
        self.save_manifest()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'manifest source paths'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())

    def test_symlink_destination_is_rejected_without_external_writes(self):
        outside = Path(self.temp.name) / 'outside.c'
        outside.write_bytes(b'keep\n')
        target = self.root / PORT['DRIVER']
        try:
            target.symlink_to(outside)
        except OSError as error:
            self.skipTest('symlink capability unavailable: ' + str(error))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'linked USB PHY'):
            APPLY(self.root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(outside.read_bytes(), b'keep\n')

    @unittest.skipUnless(shutil.which('cc'), 'requires a host C compiler for fault injection')
    def test_generated_init_propagates_bus_and_timeout_failures_and_balances_supply(self):
        # Compile the real generated routines, substituting only their hardware
        # interfaces. This exercises failure paths; it does not simulate a SoC.
        source = PORT['prepare_driver']((PORT['SOURCE_ROOT'] / PORT['DRIVER']).read_text())
        constants = source[source.index('struct sprd_ssphy {'):
                           source.index('static int sprd_ssphy_reset_core')]
        functions = []
        for name in ('sprd_ssphy_reset_core', 'usb_config_4_cts',
                     'sprd_ssphy_power_off', 'sprd_ssphy_init', 'sprd_ssphy_shutdown'):
            match = re.search(r'(?m)^static [^\n]+\b' + name +
                              r'\([^;]*?\)\n\{[\s\S]*?^}\n', source)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <errno.h>
typedef uint32_t u32;
typedef int atomic_t;
#define __iomem
#define BIT(n) (1U << (n))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define atomic_read(p) (*(p))
#define atomic_set(p,v) (*(p)=(v))
#define dev_dbg(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
#define dev_warn(...) ((void)0)
#define usleep_range(...) ((void)0)
#define msleep(...) ((void)0)
struct device { int unused; };
struct usb_phy { struct device *dev; };
struct regmap { int unused; };
struct regulator { int unused; };
static int calls, fail_at, enable_count, fail_enable, fail_disable, sram_timeout, tca_mode;
static int bus(void) { return ++calls == fail_at ? -EIO : 0; }
static int regmap_read(struct regmap *m, u32 o, u32 *v) { *v=0; return bus(); }
static int regmap_write(struct regmap *m, u32 o, u32 v) { return bus(); }
static int regmap_update_bits(struct regmap *m, u32 o, u32 b, u32 v) { return bus(); }
static int regulator_enable(struct regulator *r) {
    if (fail_enable) return -EIO;
    ++enable_count; return 0;
}
static int regulator_disable(struct regulator *r) {
    if (fail_disable) return -EIO;
    --enable_count; return 0;
}
static u32 readl_relaxed(void *a) { return 0; }
static void writel_relaxed(u32 v, void *a) { }
static int sram_poll(u32 *v) {
    int ret=bus(); if (ret) return ret;
    *v = BIT(2); return sram_timeout ? -ETIMEDOUT : 0;
}
static int tca_poll(u32 *v) { *v = tca_mode == 2 ? BIT(1) : BIT(0); return tca_mode == 1 ? -ETIMEDOUT : 0; }
#define regmap_read_poll_timeout(m,o,v,c,d,t) sram_poll(&(v))
#define readl_relaxed_poll_timeout(a,v,c,d,t) tca_poll(&(v))
'''
        main = r'''
static struct device dev;
static struct regmap map;
static struct regulator supply;
static unsigned char registers[256];
static struct sprd_ssphy fresh(void) {
    return (struct sprd_ssphy){.dev=&dev, .phy={.dev=&dev}, .base=registers,
      .aon_apb=&map, .ipa_apb=&map, .ipa_dispc1_glb_apb=&map,
      .ipa_usb31_dp=&map, .ipa_usb31_dptx=&map, .ana_g0l=&map, .vdd=&supply};
}
static void reset_state(void) {
    calls=fail_at=enable_count=fail_enable=fail_disable=sram_timeout=tca_mode=0;
}
int main(void) {
    int count;
    struct sprd_ssphy p;
    reset_state(); p=fresh();
    assert(sprd_ssphy_init(&p.phy)==0);
    count=calls;
    assert(p.inited && enable_count==1);
    assert(sprd_ssphy_init(&p.phy)==0 && enable_count==1);
    sprd_ssphy_shutdown(&p.phy);
    assert(!p.inited && !p.vdd_enabled && enable_count==0);
    sprd_ssphy_shutdown(&p.phy); assert(enable_count==0);
    for (int i=1;i<=count;i++) {
        reset_state(); p=fresh(); fail_at=i;
        assert(sprd_ssphy_init(&p.phy)==-EIO);
        assert(!p.inited && !p.vdd_enabled && enable_count==0);
    }
    reset_state(); p=fresh(); fail_enable=1;
    assert(sprd_ssphy_init(&p.phy)==-EIO && !calls && !enable_count && !p.inited);
    reset_state(); p=fresh(); sram_timeout=1;
    assert(sprd_ssphy_init(&p.phy)==-ETIMEDOUT && !p.inited && !enable_count);
    for (int mode=1;mode<=2;mode++) {
        reset_state(); p=fresh(); tca_mode=mode;
        assert(sprd_ssphy_init(&p.phy)==-ETIMEDOUT && !p.inited && !enable_count);
    }
    reset_state(); p=fresh(); fail_at=1; fail_disable=1;
    assert(sprd_ssphy_init(&p.phy)==-EIO && !p.inited && p.vdd_enabled && enable_count==1);
    fail_at=fail_disable=0;
    assert(sprd_ssphy_init(&p.phy)==0 && enable_count==1);
    sprd_ssphy_shutdown(&p.phy); assert(enable_count==0 && !p.vdd_enabled);
    return 0;
}
'''
        headers = '\n'.join((PORT['SOURCE_ROOT'] / name).read_text()
                            for name in PORT['FILES'][1:])
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / 'fault-injection'
            compiled = subprocess.run(
                ['cc', '-x', 'c', '-std=gnu11', '-O0', '-o', str(program), '-'],
                input=preamble + headers + constants + '\n'.join(functions) + main,
                text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            executed = subprocess.run([str(program)], text=True, capture_output=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)


if __name__ == '__main__':
    unittest.main()
