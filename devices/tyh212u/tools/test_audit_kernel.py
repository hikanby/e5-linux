import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'audit-kernel.py'
SPEC = importlib.util.spec_from_file_location('tyh212u_audit_kernel', SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        git(self.root, 'init', '-q')
        git(self.root, 'config', 'user.name', 'Synthetic Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        (self.root / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 54\n')
        directory = self.root / 'drivers/power/supply'
        directory.mkdir(parents=True)
        (directory / 'Kconfig').write_text('config CHARGER_SGM41511\n\ttristate "fixture"\n')
        (directory / 'Makefile').write_text('obj-$(CONFIG_CHARGER_SGM41511) += fixture.o\n')
        (directory / 'fixture.c').write_text('''
/* .compatible = "sc,sc8551-standalone" */
const char *unrelated = "Sgm,sgm41511_chg";
static const struct of_device_id match[] = {
  { .compatible = "Sgm,sgm41511_chg" },
};
// .name = "omnivision_tcm_spi"
static struct driver fake = { .name = "sgm41511_chg" };
''')
        self.commit()

    def tearDown(self):
        self.temp.cleanup()

    def commit(self):
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-q', '-m', 'Synthetic source fixture')
        self.revision = git(self.root, 'rev-parse', 'HEAD')

    def test_exact_tokens_and_comments_are_not_hardware_proof(self):
        result = audit.audit_kernel(self.root, expected_revision=self.revision)
        by_name = {entry['component']: entry for entry in result['components']}
        sgm = by_name['sgm41511_charger']
        self.assertEqual(len(sgm['exact_compatible_assignments']['Sgm,sgm41511_chg']), 1)
        self.assertTrue(sgm['kconfig_definitions']['CHARGER_SGM41511'])
        self.assertFalse(by_name['sc8551_charger']['exact_compatible_assignments']['sc,sc8551-standalone'])
        self.assertFalse(by_name['omnivision_tcm_touch']['exact_driver_name_assignments']['omnivision_tcm_spi'])
        self.assertEqual(result['audit_kind'], 'textual_source_matches_only')
        self.assertFalse(result['status']['hardware_validated'])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_wrong_revision_rejected(self):
        with self.assertRaisesRegex(audit.AuditError, 'revision'):
            audit.audit_kernel(self.root)

    def test_dirty_and_untracked_inputs_rejected(self):
        (self.root / 'untracked').write_text('new')
        with self.assertRaisesRegex(audit.AuditError, 'clean'):
            audit.audit_kernel(self.root, expected_revision=self.revision)

    def test_wrong_kernel_version_rejected(self):
        (self.root / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 55\n')
        self.commit()
        with self.assertRaisesRegex(audit.AuditError, 'version'):
            audit.audit_kernel(self.root, expected_revision=self.revision)

    def test_subdirectory_cannot_masquerade_as_checkout_root(self):
        with self.assertRaisesRegex(audit.AuditError, 'root'):
            audit.verified_checkout(self.root / 'drivers', self.revision)

    def test_source_link_outside_checkout_is_rejected(self):
        try:
            (self.root / 'drivers/power/supply/linked.c').symlink_to(SCRIPT)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create file symlinks')
        self.commit()
        with self.assertRaisesRegex(audit.AuditError, 'symlink'):
            audit.audit_kernel(self.root, expected_revision=self.revision)

    def test_prefix_or_comment_kconfig_is_not_an_exact_match(self):
        index = audit.index_source({'drivers/power/supply/Kconfig':
                                    '# config CHARGER_SC8551\nconfig CHARGER_SC8551_EXTRA\n'})
        self.assertNotIn('CHARGER_SC8551', index['kconfig'])
        self.assertIn('CHARGER_SC8551_EXTRA', index['kconfig'])

    def test_reported_line_numbers_survive_comments_and_blank_lines(self):
        index = audit.index_source({
            'drivers/power/supply/Kconfig.test': '\n\nconfig CHARGER_SC8551\n',
            'drivers/power/supply/Makefile': '# a long comment\n\nobj-$(CONFIG_CHARGER_SC8551) += test.o\n',
        })
        self.assertEqual(index['kconfig']['CHARGER_SC8551'][0]['line'], 3)
        self.assertEqual(index['makefile_config']['CHARGER_SC8551'][0]['line'], 3)


if __name__ == '__main__':
    unittest.main()
