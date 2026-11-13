import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'tyh_dwc3', Path(__file__).resolve().parents[1] / 'dwc3.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


SOURCE_MANIFEST = json.loads((driver.HERE / 'port/dwc3-sources.json').read_text())
# SPDX-License-Identifier: GPL-2.0
# Exact functions from the pinned Linux core.c, retained for generated-C testing.
BASELINE_CORE = b'''static int dwc3_phy_init(struct dwc3 *dwc)
{
	int ret;
	int i;
	int j;

	usb_phy_init(dwc->usb2_phy);
	usb_phy_init(dwc->usb3_phy);

	for (i = 0; i < dwc->num_usb2_ports; i++) {
		ret = phy_init(dwc->usb2_generic_phy[i]);
		if (ret < 0)
			goto err_exit_usb2_phy;
	}

	for (j = 0; j < dwc->num_usb3_ports; j++) {
		ret = phy_init(dwc->usb3_generic_phy[j]);
		if (ret < 0)
			goto err_exit_usb3_phy;
	}

	/*
	 * Above DWC_usb3.0 1.94a, it is recommended to set
	 * DWC3_GUSB3PIPECTL_SUSPHY and DWC3_GUSB2PHYCFG_SUSPHY to '0' during
	 * coreConsultant configuration. So default value will be '0' when the
	 * core is reset. Application needs to set it to '1' after the core
	 * initialization is completed.
	 *
	 * Certain phy requires to be in P0 power state during initialization.
	 * Make sure GUSB3PIPECTL.SUSPENDENABLE and GUSB2PHYCFG.SUSPHY are clear
	 * prior to phy init to maintain in the P0 state.
	 *
	 * After phy initialization, some phy operations can only be executed
	 * while in lower P states. Ensure GUSB3PIPECTL.SUSPENDENABLE and
	 * GUSB2PHYCFG.SUSPHY are set soon after initialization to avoid
	 * blocking phy ops.
	 */
	if (!DWC3_VER_IS_WITHIN(DWC3, ANY, 194A))
		dwc3_enable_susphy(dwc, true);

	return 0;

err_exit_usb3_phy:
	while (--j >= 0)
		phy_exit(dwc->usb3_generic_phy[j]);

err_exit_usb2_phy:
	while (--i >= 0)
		phy_exit(dwc->usb2_generic_phy[i]);

	usb_phy_shutdown(dwc->usb3_phy);
	usb_phy_shutdown(dwc->usb2_phy);

	return ret;
}


static void dwc3_phy_exit(struct dwc3 *dwc)
{
	int i;

	for (i = 0; i < dwc->num_usb3_ports; i++)
		phy_exit(dwc->usb3_generic_phy[i]);

	for (i = 0; i < dwc->num_usb2_ports; i++)
		phy_exit(dwc->usb2_generic_phy[i]);

	usb_phy_shutdown(dwc->usb3_phy);
	usb_phy_shutdown(dwc->usb2_phy);
}
'''


class Dwc3PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'kernel'
        self.path = self.root / driver.TARGET
        self.path.parent.mkdir(parents=True)
        self.baseline = b'/* baseline */\n' + driver.ANCHOR + b'\n'
        self.prepared = driver.prepare(self.baseline)
        self.path.write_bytes(self.baseline)
        self.core_path = self.root / driver.CORE_TARGET
        self.core_prepared = driver.prepare_core(BASELINE_CORE)
        self.core_path.write_bytes(BASELINE_CORE)
        files = {
            driver.TARGET: {
                'baseline_sha256': hashlib.sha256(self.baseline).hexdigest(),
                'prepared_sha256': hashlib.sha256(self.prepared).hexdigest(),
            },
            driver.CORE_TARGET: {
                'baseline_sha256': hashlib.sha256(BASELINE_CORE).hexdigest(),
                'prepared_sha256': hashlib.sha256(self.core_prepared).hexdigest(),
            },
        }
        manifest = {
            'files': files,
            'prepared_files': {name: record['prepared_sha256']
                               for name, record in files.items()},
        }
        self.manifest = self.root / 'port/dwc3-sources.json'
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps(manifest))
        mock_here = patch.object(driver, 'HERE', self.root)
        mock_here.start()
        self.addCleanup(mock_here.stop)

    def test_idempotence_preserves_all_other_source_bytes(self):
        driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.prepared)
        self.assertEqual(self.core_path.read_bytes(), self.core_prepared)
        driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.prepared)
        self.assertEqual(self.core_path.read_bytes(), self.core_prepared)
        self.assertEqual(self.prepared.replace(driver.ADDITION, b''), self.baseline)
        self.assertEqual(self.core_prepared.replace(
            driver.CORE_INIT_AFTER, driver.CORE_INIT_BEFORE).replace(
            driver.CORE_UNWIND_AFTER, driver.CORE_UNWIND_BEFORE), BASELINE_CORE)

    def test_foreign_or_tampered_prepared_source_is_preserved(self):
        for source in (self.baseline + b'foreign\n', self.prepared + b'foreign\n'):
            self.path.write_bytes(source)
            with self.assertRaisesRegex(ValueError, 'pinned DWC3 baseline'):
                driver.apply(self.root)
            self.assertEqual(self.path.read_bytes(), source)

    def test_prepared_hash_is_checked_before_writes(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['files'][driver.CORE_TARGET]['prepared_sha256'] = '0' * 64
        manifest['prepared_files'][driver.CORE_TARGET] = '0' * 64
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Prepared DWC3 source hash'):
            driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.baseline)
        self.assertEqual(self.core_path.read_bytes(), BASELINE_CORE)

    def test_wrong_table_and_repeated_compatible_are_rejected(self):
        for source in (b'no compatible table', self.baseline * 2, self.prepared):
            with self.assertRaisesRegex(ValueError, 'compatible table'):
                driver.prepare(source)

    def test_foreign_core_is_rejected_before_any_writes(self):
        source = BASELINE_CORE + b'/* foreign */\n'
        self.core_path.write_bytes(source)
        with self.assertRaisesRegex(ValueError, 'pinned DWC3 baseline'):
            driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.baseline)
        self.assertEqual(self.core_path.read_bytes(), source)

    def test_wrong_or_already_prepared_core_is_rejected(self):
        for source in (b'no PHY init', BASELINE_CORE * 2,
                       BASELINE_CORE.replace(driver.CORE_INIT_BEFORE, b'wrong init'),
                       self.core_prepared):
            with self.assertRaisesRegex(ValueError, 'baseline DWC3 PHY init'):
                driver.prepare_core(source)

    def test_fixture_is_the_actual_pinned_init_function_and_generated_output(self):
        for source, expected in (
                (BASELINE_CORE, SOURCE_MANIFEST['legacy_phy_init_function']['baseline_sha256']),
                (self.core_prepared, SOURCE_MANIFEST['legacy_phy_init_function']['prepared_sha256'])):
            function = driver.phy_init_function(source).group()
            self.assertEqual(hashlib.sha256(function).hexdigest(), expected)

    def test_manifest_paths_and_output_summary_cannot_be_redirected(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['prepared_files']['../outside.c'] = '0' * 64
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'DWC3 source paths'):
            driver.apply(self.root)
        self.assertEqual(self.path.read_bytes(), self.baseline)
        self.assertEqual(self.core_path.read_bytes(), BASELINE_CORE)

    def test_linked_destination_is_rejected_without_external_writes(self):
        external = Path(self.temp.name) / 'external.c'
        external.write_bytes(self.baseline)
        self.path.unlink()
        try:
            self.path.symlink_to(external)
        except OSError as error:
            self.skipTest('Host cannot create symbolic links: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'linked DWC3 destination'):
            driver.apply(self.root)
        self.assertEqual(external.read_bytes(), self.baseline)

    @unittest.skipUnless(shutil.which('cc'), 'requires a host C compiler for fault injection')
    def test_generated_c_preserves_reference_pairing_and_unwinds_each_failure(self):
        # Run the actual prepared Linux routine and its unchanged exit routine.
        # These mocks observe callback ordering; they do not model register rollback.
        source = driver.prepare_core(BASELINE_CORE).decode()
        init = driver.phy_init_function(source.encode()).group().decode()
        exit_function = re.search(
            r'(?m)^static void dwc3_phy_exit\(struct dwc3 \*dwc\)\n\{[\s\S]*?^}\n',
            source).group()
        preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
struct usb_phy {
    int id, calls, successes, shutdowns, fail_on_call;
    bool inited;
};
struct phy { int id, calls, exits, fail; };
struct dwc3 {
    struct usb_phy *usb2_phy, *usb3_phy;
    int num_usb2_ports, num_usb3_ports, susphy;
    struct phy *usb2_generic_phy[2], *usb3_generic_phy[2];
};
static int events[32], event_count;
static void record(int event) { assert(event_count < 32); events[event_count++] = event; }
static int usb_phy_init(struct usb_phy *p) {
    if (!p) return 0;
    record(10 + p->id);
    if (++p->calls == p->fail_on_call) return -EIO;
    ++p->successes;
    /* Model the SSPHY's idempotent callback, without generic refcount assumptions. */
    p->inited = true;
    return 0;
}
static void usb_phy_shutdown(struct usb_phy *p) {
    if (!p) return;
    record(20 + p->id); ++p->shutdowns; p->inited = false;
}
static int phy_init(struct phy *p) {
    if (!p) return 0;
    record(100 + p->id); ++p->calls;
    return p->fail ? -EAGAIN : 0;
}
static void phy_exit(struct phy *p) {
    if (!p) return;
    record(200 + p->id); ++p->exits;
}
#define DWC3_VER_IS_WITHIN(a,b,c) 0
static void dwc3_enable_susphy(struct dwc3 *dwc, bool enable) {
    assert(enable); ++dwc->susphy;
}
'''
        main = r'''
static void expect(const int *expected, int count) {
    assert(event_count == count);
    assert(!memcmp(events, expected, count * sizeof(int)));
    event_count = 0;
}
#define EXPECT(...) do { int expected[] = {__VA_ARGS__}; \
    expect(expected, sizeof(expected) / sizeof(expected[0])); } while (0)
int main(void) {
    struct usb_phy a, b;
    struct phy generic[4];
    struct dwc3 d;
    /* Distinct legacy PHYs keep the original ordering and one callback per reference. */
    a=(struct usb_phy){.id=1}; b=(struct usb_phy){.id=2};
    d=(struct dwc3){.usb2_phy=&a, .usb3_phy=&b};
    assert(!dwc3_phy_init(&d) && d.susphy==1);
    dwc3_phy_exit(&d);
    assert(a.successes==1 && a.shutdowns==1 && !a.inited);
    assert(b.successes==1 && b.shutdowns==1 && !b.inited);
    EXPECT(11,12,22,21);
    /* First failure has no completed legacy or generic callback to unwind. */
    a=(struct usb_phy){.id=1,.fail_on_call=1}; b=(struct usb_phy){.id=2};
    d=(struct dwc3){.usb2_phy=&a, .usb3_phy=&b};
    assert(dwc3_phy_init(&d)==-EIO && !d.susphy);
    assert(!a.shutdowns && !b.calls);
    EXPECT(11);
    /* Second failure only shuts down the successful USB2 reference. */
    a=(struct usb_phy){.id=1}; b=(struct usb_phy){.id=2,.fail_on_call=1};
    d=(struct dwc3){.usb2_phy=&a, .usb3_phy=&b};
    assert(dwc3_phy_init(&d)==-EIO && !d.susphy);
    assert(a.successes==1 && a.shutdowns==1 && !b.shutdowns);
    EXPECT(11,12,21);
    /* Two references to our shared idempotent PHY preserve both baseline callbacks. */
    a=(struct usb_phy){.id=1};
    d=(struct dwc3){.usb2_phy=&a,.usb3_phy=&a};
    assert(!dwc3_phy_init(&d) && a.inited && a.successes==2);
    dwc3_phy_exit(&d);
    assert(a.calls==2 && a.shutdowns==2 && !a.inited);
    EXPECT(11,11,21,21);
    for (int call=1;call<=2;call++) {
        a=(struct usb_phy){.id=1,.fail_on_call=call};
        d=(struct dwc3){.usb2_phy=&a,.usb3_phy=&a};
        assert(dwc3_phy_init(&d)==-EIO && !d.susphy);
        assert(a.successes==call-1 && a.shutdowns==call-1 && !a.inited);
        if (call==1) EXPECT(11); else EXPECT(11,11,21);
    }
    /* The real legacy wrappers treat absent references as successful no-ops. */
    d=(struct dwc3){0};
    assert(!dwc3_phy_init(&d)); dwc3_phy_exit(&d); assert(!event_count);
    a=(struct usb_phy){.id=1};
    d=(struct dwc3){.usb2_phy=&a};
    assert(!dwc3_phy_init(&d)); dwc3_phy_exit(&d);
    EXPECT(11,21);
    b=(struct usb_phy){.id=2};
    d=(struct dwc3){.usb3_phy=&b};
    assert(!dwc3_phy_init(&d)); dwc3_phy_exit(&d);
    EXPECT(12,22);
    b=(struct usb_phy){.id=2,.fail_on_call=1};
    d=(struct dwc3){.usb3_phy=&b};
    assert(dwc3_phy_init(&d)==-EIO && !b.shutdowns);
    EXPECT(12);
    /* Each generic failure unwinds only prior successes, then both legacy references. */
    for (int fail=0;fail<4;fail++) {
        a=(struct usb_phy){.id=1}; b=(struct usb_phy){.id=2};
        for (int i=0;i<4;i++) generic[i]=(struct phy){.id=i+1,.fail=i==fail};
        d=(struct dwc3){.usb2_phy=&a,.usb3_phy=&b,.num_usb2_ports=2,.num_usb3_ports=2,
            .usb2_generic_phy={&generic[0],&generic[1]},
            .usb3_generic_phy={&generic[2],&generic[3]}};
        assert(dwc3_phy_init(&d)==-EAGAIN && !d.susphy);
        assert(a.shutdowns==1 && b.shutdowns==1);
        for (int i=0;i<4;i++) assert(generic[i].exits==(i<fail));
        if (fail==0) EXPECT(11,12,101,22,21);
        if (fail==1) EXPECT(11,12,101,102,201,22,21);
        if (fail==2) EXPECT(11,12,101,102,103,202,201,22,21);
        if (fail==3) EXPECT(11,12,101,102,103,104,203,202,201,22,21);
    }
    /* Generic failure after both shared callbacks still balances both references. */
    a=(struct usb_phy){.id=1}; generic[0]=(struct phy){.id=1,.fail=1};
    d=(struct dwc3){.usb2_phy=&a,.usb3_phy=&a,.num_usb2_ports=1,
        .usb2_generic_phy={&generic[0]}};
    assert(dwc3_phy_init(&d)==-EAGAIN && a.successes==2 && a.shutdowns==2 && !a.inited);
    EXPECT(11,11,101,21,21);
    /* Successful generic PHYs retain the original normal-exit ordering. */
    a=(struct usb_phy){.id=1}; b=(struct usb_phy){.id=2};
    for (int i=0;i<4;i++) generic[i]=(struct phy){.id=i+1};
    d=(struct dwc3){.usb2_phy=&a,.usb3_phy=&b,.num_usb2_ports=2,.num_usb3_ports=2,
        .usb2_generic_phy={&generic[0],&generic[1]},
        .usb3_generic_phy={&generic[2],&generic[3]}};
    assert(!dwc3_phy_init(&d)); dwc3_phy_exit(&d);
    EXPECT(11,12,101,102,103,104,203,204,201,202,22,21);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / 'dwc3-fault-injection'
            compiled = subprocess.run(
                ['cc', '-x', 'c', '-std=gnu11', '-O0', '-o', str(program), '-'],
                input=preamble + init + exit_function + main,
                text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            executed = subprocess.run([str(program)], text=True, capture_output=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)


if __name__ == '__main__':
    unittest.main()
