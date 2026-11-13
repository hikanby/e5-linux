#!/usr/bin/env python3
"""Prepare the USB PHY hardware component for compilation, not board operation.

There is deliberately no PMIC DPDM routing, BC1.2 policy, Type-C/OTG boost
policy or DT rewriting. Those must be adapted before this can be used on a
phone. The cached M8 import supplies source bytes, never a board profile.
"""
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE_ROOT = REPO / 'devices/m8/port'
MANIFEST = HERE / 'port/usb-phy-sources.json'
DRIVER = 'drivers/usb/phy/phy-sprd-ums9620-ssphy.c'
FILES = (DRIVER, 'include/dt-bindings/soc/sprd,qogirn6pro-mask.h',
         'include/dt-bindings/soc/sprd,qogirn6pro-regs.h')
API_FILES = ('include/linux/usb/phy.h', 'include/linux/iopoll.h')
KCONFIG = '''config USB_SPRD_UMS9620_SSPHY
\ttristate "Experimental Unisoc UMS9620 USB PHY hardware"
\tdepends on ARCH_SPRD && OF && REGULATOR
\tselect USB_PHY
\tselect MFD_SYSCON
\thelp
\t  Compile the UMS9620 combo PHY hardware component. TYH212U PMIC
\t  DPDM, charger detection and Type-C/OTG board policy are not
\t  implemented. This is not sufficient to operate USB on a phone.
'''
MAKEFILE = 'obj-$(CONFIG_USB_SPRD_UMS9620_SSPHY) += phy-sprd-ums9620-ssphy.o\n'


def _checked_regmap(body, failure):
    """Preserve the donor's operation order but check each bus operation."""
    pattern = r'(?m)^([ \t]*)(?:ret \|= )?(regmap_(?:read|write|update_bits)\([^;]+;)[ \t]*$'
    def checked(match):
        indent, call = match.groups()
        return (indent + 'ret = ' + call + '\n' + indent + 'if (ret)\n' +
                indent + '\t' + failure)
    return re.sub(pattern, checked, body)


def prepare_driver(text):
    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError('Unexpected USB PHY source anchor: ' + old[:70])
        text = text.replace(old, new)

    def function(name, update):
        nonlocal text
        match = re.search(r'(?m)^static [^\n]+\b' + re.escape(name) +
                          r'\([\s\S]*?^}\n', text, re.M)
        if not match:
            raise ValueError('Missing USB PHY function: ' + name)
        text = text[:match.start()] + update(match.group()) + text[match.end():]

    replace(' * peripheral mode and D+/D- are switched from the PMIC charger detector to the PHY at probe.',
            ' * compilation only. PMIC DPDM and charging/role coordination remain unimplemented.')
    replace('#include <linux/io.h>', '#include <linux/io.h>\n#include <linux/iopoll.h>')
    replace('#include <linux/of_platform.h>\n', '')
    replace('\tstruct regmap\t\t*pmic;\n', '')
    replace('\tbool\t\t\tis_host;', '\tbool\t\t\tis_host;\n\tbool\t\t\tvdd_enabled;')
    replace('#define CHGR_DET_FGU_CTRL\t\t0x23a0\n'
            '#define DP_DM_FS_ENB\t\t\tBIT(14)\n'
            '#define DP_DM_BC_ENB\t\t\tBIT(0)\n', '')
    function('sc27xx_dpdm_switch_to_phy', lambda body: '')
    function('sprd_ssphy_pmic_regmap', lambda body: '')
    start = text.index('/*\n * Where VBUS comes from.')
    end = text.index('/*\n * dwc3 tells the PHY', start)
    text = text[:start] + text[end:]
    replace('\tphy->pmic = sprd_ssphy_pmic_regmap(dev);\n'
            '\tif (!phy->pmic)\n'
            '\t\treturn dev_err_probe(dev, -EPROBE_DEFER, "PMIC regmap not ready\\n");\n\n', '')
    replace('\tsprd_ssphy_vbus_source(dev);\n', '')
    replace('\t/* device mode only for now: route D+/D- from the charger detector to the USB PHY */\n'
            '\tsc27xx_dpdm_switch_to_phy(phy->pmic, true);\n', '')

    def reset(body):
        body = body.replace('static inline void', 'static int')
        return _checked_regmap(body, 'return ret;').removesuffix('}\n') + '\treturn 0;\n}\n'
    function('sprd_ssphy_reset_core', reset)
    function('sprd_ssphy_set_vbus', lambda body: _checked_regmap(body, 'return ret;'))

    def cts(body):
        body = body.replace('static void', 'static int').replace('u32\treg;', 'u32\treg;\n\tint ret;')
        return _checked_regmap(body, 'return ret;').removesuffix('}\n') + '\treturn 0;\n}\n'
    function('usb_config_4_cts', cts)

    def init(body):
        body = body.replace('\tint timeout;\n', '')
        start = body.index('\t/*\n\t * Due to chip design,')
        end = body.index('\t/* select the IPA_SYS', start)
        body = body[:start] + '''\tif (!phy->vdd_enabled) {
\t\tret = regulator_enable(phy->vdd);
\t\tif (ret)
\t\t\treturn ret;
\t\tphy->vdd_enabled = true;
\t}

''' + body[end:]
        start = body.index('\t/* wait phy_sram init')
        end = body.index('\t/* REG_TYPEC_CTRL', start)
        body = body[:start] + '''\tret = regmap_read_poll_timeout(phy->ipa_usb31_dp,
\t\t\tPHY_SRAM_INIT_CHECK_DONE, reg, reg & BIT(2),
\t\t\t1000, PHY_INIT_TIMEOUT * 1000);
\tif (ret)
\t\tgoto err_power_off;

\tret = usb_config_4_cts(phy);
\tif (ret)
\t\tgoto err_power_off;

''' + body[end:]
        start = body.index('\t/* wait tca interrupt */')
        end = body.index('\tatomic_set(&phy->inited, 1);', start)
        body = body[:start] + '''\tret = readl_relaxed_poll_timeout(phy->base + 0x08, reg,
\t\t\treg & (BIT(0) | BIT(1)), 1000, PHY_INIT_TIMEOUT * 1000);
\tif (ret)
\t\tgoto err_power_off;
\twritel_relaxed(0xffff, phy->base + 0x08);
\t/* BIT(1) is a TCA timeout event, not a successful acknowledgement. */
\tif (reg & BIT(1)) {
\t\tret = -ETIMEDOUT;
\t\tgoto err_power_off;
\t}

''' + body[end:]
        body = body.replace('\tsprd_ssphy_reset_core(phy);',
                            '\tret = sprd_ssphy_reset_core(phy);\n\tif (ret)\n\t\tgoto err_power_off;')
        body = _checked_regmap(body, 'goto err_power_off;')
        return body.replace('\treturn ret;\n}', '''\treturn 0;

err_power_off:
\tdev_err(x->dev, "PHY initialization failed: %d\\n", ret);
\tsprd_ssphy_power_off(phy);
\treturn ret;
}''')
    function('sprd_ssphy_init', init)
    replace('static int sprd_ssphy_init(', 'static void sprd_ssphy_power_off(struct sprd_ssphy *phy);\n\nstatic int sprd_ssphy_init(')

    def shutdown(body):
        body = body.replace('static void sprd_ssphy_shutdown(struct usb_phy *x)',
                            'static void sprd_ssphy_power_off(struct sprd_ssphy *phy)')
        body = body.replace('\tstruct sprd_ssphy *phy = container_of(x, struct sprd_ssphy, phy);\n', '')
        start = body.index('\tif (!atomic_read(&phy->inited))')
        end = body.index('\n\n\n\t\tif (true)', start)
        body = body[:start] + body[end:]
        body = body.replace('\tu32 msk = 0, reg = 0;', '\tu32 msk = 0, reg = 0;\n\tint ret;')
        body = body.replace('''\tregmap_read(phy->ipa_dispc1_glb_apb, DISPC1_GLB_APB_RST, &reg);
\treg |= BIT(4);
\tregmap_write(phy->ipa_dispc1_glb_apb, DISPC1_GLB_APB_RST, reg);''',
                            '\tregmap_update_bits(phy->ipa_dispc1_glb_apb, DISPC1_GLB_APB_RST, BIT(4), BIT(4));')
        start = body.index('\t/*\n\t * Due to chip design,')
        end = body.index('\tatomic_set(&phy->inited, 0);', start)
        body = body[:start] + '''\t/* Balance this consumer's enable, never another consumer's reference. */
\tif (phy->vdd_enabled) {
\t\tret = regulator_disable(phy->vdd);
\t\tif (ret)
\t\t\tdev_warn(phy->dev, "cannot disable PHY supply: %d\\n", ret);
\t\telse
\t\t\tphy->vdd_enabled = false;
\t}

''' + body[end:]
        body = _checked_regmap(body, 'dev_warn(phy->dev, "PHY power-off register error: %d\\n", ret);')
        return body + '''
static void sprd_ssphy_shutdown(struct usb_phy *x)
{
\tstruct sprd_ssphy *phy = container_of(x, struct sprd_ssphy, phy);

\tif (atomic_read(&phy->inited) || phy->vdd_enabled)
\t\tsprd_ssphy_power_off(phy);
}
'''
    function('sprd_ssphy_shutdown', shutdown)
    for name in ('sprd_ssphy_notify_connect', 'sprd_ssphy_notify_disconnect'):
        function(name, lambda body: _checked_regmap(
            body.replace('\tu32 msk = 0, reg = 0;', '\tu32 msk = 0, reg = 0;\n\tint ret;').replace(
                '\tu32 msk = 0;', '\tu32 msk = 0;\n\tint ret;'), 'return ret;'))
    replace('''\tstruct usb_phy *x = container_of(nb, struct usb_phy, vbus_nb);

\tdev_info(x->dev, "vbus %s\\n", event ? "on" : "off");
\tif (event)
\t\tsprd_ssphy_notify_connect(x, USB_SPEED_UNKNOWN);
\telse
\t\tsprd_ssphy_notify_disconnect(x, USB_SPEED_UNKNOWN);
\treturn NOTIFY_OK;''', '''\tstruct usb_phy *x = container_of(nb, struct usb_phy, vbus_nb);
\tint ret;

\tif (event)
\t\tret = sprd_ssphy_notify_connect(x, USB_SPEED_UNKNOWN);
\telse
\t\tret = sprd_ssphy_notify_disconnect(x, USB_SPEED_UNKNOWN);
\treturn notifier_from_errno(ret);''')
    replace('''\tphy->vdd = devm_regulator_get_optional(dev, "vdd");
\tif (IS_ERR(phy->vdd)) {
\t\tif (PTR_ERR(phy->vdd) == -EPROBE_DEFER)
\t\t\treturn -EPROBE_DEFER;
\t\tdev_warn(dev, "no vdd supply\\n");
\t\tphy->vdd = NULL;
\t} else if (regulator_set_voltage(phy->vdd, phy->vdd_vol, phy->vdd_vol)) {
\t\tdev_warn(dev, "cannot set vdd to %u uV\\n", phy->vdd_vol);
\t}''', '''\tphy->vdd = devm_regulator_get(dev, "vdd");
\tif (IS_ERR(phy->vdd))
\t\treturn dev_err_probe(dev, PTR_ERR(phy->vdd), "missing vdd supply\\n");
\tret = regulator_set_voltage(phy->vdd, phy->vdd_vol, phy->vdd_vol);
\tif (ret)
\t\treturn dev_err_probe(dev, ret, "cannot set vdd to %u uV\\n", phy->vdd_vol);''')
    replace('''\t/* select the IPA_SYS USB controller */
\tregmap_update_bits(phy->aon_apb, REG_AON_APB_AON_SOC_USB_CTRL,
\t\t\t   MASK_AON_APB_USB20_CTRL_MUX_REG, MASK_AON_APB_USB20_CTRL_MUX_REG);

''', '')
    replace('\tusb_remove_phy(&phy->phy);', '\tusb_remove_phy(&phy->phy);\n\tsprd_ssphy_shutdown(&phy->phy);')
    replace('.name = "sprd-ssphy"', '.name = "sprd-tyh212u-ssphy-experimental"')
    replace('MODULE_DESCRIPTION("Unisoc UMS9620 USB 3.1 PHY driver");',
            'MODULE_DESCRIPTION("Experimental UMS9620 USB PHY hardware; board policy unadapted");')
    return '\n'.join(line.rstrip() for line in text.splitlines()) + '\n'


def _target(kernel, name):
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Invalid USB PHY destination')
    path = kernel
    for part in relative.parts:
        path /= part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Refusing linked USB PHY destination')
        if path.exists() and path != kernel / relative and not path.is_dir():
            raise ValueError('USB PHY destination parent is not a directory')
    if not path.resolve().is_relative_to(kernel):
        raise ValueError('USB PHY destination escapes kernel')
    if path.exists() and not path.is_file():
        raise ValueError('USB PHY destination is not a regular file')
    return path


def _append(data, marker, addition, reference):
    if marker in data:
        if data.count(marker) != 1 or data.count(addition) != 1:
            raise ValueError('Conflicting USB PHY build entry')
        if reference and data.count(reference) != addition.count(reference):
            raise ValueError('Conflicting USB PHY object rule')
        return data
    if reference and reference in data:
        raise ValueError('Conflicting USB PHY object rule')
    return data + b'\n' + addition


def apply(kernel):
    kernel = Path(kernel)
    if kernel.is_symlink() or getattr(kernel, 'is_junction', lambda: False)():
        raise ValueError('Refusing linked USB PHY kernel root')
    kernel = kernel.resolve(strict=True)
    if not kernel.is_dir():
        raise ValueError('Expected a USB PHY kernel directory')
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if (set(manifest['files']) != set(FILES) or
            set(manifest['prepared_files']) != set(FILES) or
            set(manifest['kernel_api_files']) != set(API_FILES)):
        raise ValueError('Unexpected USB PHY manifest source paths')
    planned = {}
    for name, expected in manifest['kernel_api_files'].items():
        if hashlib.sha256(_target(kernel, name).read_bytes()).hexdigest() != expected:
            raise ValueError('Expected pinned USB PHY kernel API: ' + name)
    for name, expected in manifest['files'].items():
        data = (SOURCE_ROOT / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('USB PHY donor hash mismatch: ' + name)
        if name == DRIVER:
            data = prepare_driver(data.decode('utf-8')).encode('utf-8')
        if hashlib.sha256(data).hexdigest() != manifest['prepared_files'][name]:
            raise ValueError('USB PHY prepared hash mismatch: ' + name)
        target = _target(kernel, name)
        if target.exists() and target.read_bytes() != data:
            raise ValueError('Foreign USB PHY destination: ' + name)
        planned[target] = data
    for name, marker, addition, reference in (
        ('drivers/usb/phy/Kconfig', b'config USB_SPRD_UMS9620_SSPHY', KCONFIG.encode(), b''),
        ('drivers/usb/phy/Makefile', b'CONFIG_USB_SPRD_UMS9620_SSPHY', MAKEFILE.encode(),
         b'phy-sprd-ums9620-ssphy.o'),
    ):
        path = _target(kernel, name)
        planned[path] = _append(path.read_bytes(), marker, addition, reference)
    for path, data in planned.items():
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
