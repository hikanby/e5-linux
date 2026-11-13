#!/usr/bin/env python3
"""Stage only the IPA power-domain component needed by TYH212U USB.

The PHY, DWC3 glue and charger/Type-C policy are intentionally not imported.
The checked-in M8 donor is used as a source container, never as a board profile.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP = HERE.parents[1]
MANIFEST = HERE / 'port/ipa/sources.json'
TARGET = Path('drivers/pmdomain/sprd/ums9620-ipa-sys.c')


def source():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    path = TOP / manifest['source_path']
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['source_sha256']:
        raise ValueError('IPA donor source hash mismatch')
    text = data.decode('utf-8')

    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError('IPA source anchor mismatch: ' + old[:80])
        text = text.replace(old, new)

    replace('static void ipa_set(struct ipa_sys_pd *pd, int i, bool set)', '''/* TYH212U stock DT packs register specifiers in syscons. */
static int ipa_get_reg(struct device_node *np, const char *name,
                       struct ipa_reg *reg)
{
\tstruct of_phandle_args args;
\tstruct regmap *map;
\tu32 values[2];
\tint index, ret;

\tif (of_find_property(np, "syscon-names", NULL)) {
\t\tindex = of_property_match_string(np, "syscon-names", name);
\t\tif (index < 0)
\t\t\treturn index;
\t\tret = of_parse_phandle_with_args(np, "syscons", "#syscon-cells",
\t\t\t\t\t index, &args);
\t\tif (ret)
\t\t\treturn ret;
\t\tif (args.args_count != ARRAY_SIZE(values)) {
\t\t\tof_node_put(args.np);
\t\t\treturn -EINVAL;
\t\t}
\t\tmap = syscon_node_to_regmap(args.np);
\t\tof_node_put(args.np);
\t\tvalues[0] = args.args[0];
\t\tvalues[1] = args.args[1];
\t} else {
\t\tmap = syscon_regmap_lookup_by_phandle_args(np, name, 2, values);
\t}
\tif (IS_ERR(map))
\t\treturn PTR_ERR(map);
\tif (!values[1])
\t\treturn -EINVAL;
\t*reg = (struct ipa_reg){ map, values[0], values[1] };
\treturn 0;
}

static void ipa_set(struct ipa_sys_pd *pd, int i, bool set)''')
    replace('\tu32 args[2];\n\tint i, ret;', '\tint i, ret;')
    replace('''\tfor (i = 0; i < NR_REGS; i++) {
\t\tstruct regmap *m = syscon_regmap_lookup_by_phandle_args(np, reg_names[i], 2, args);

\t\tif (IS_ERR(m)) {
\t\t\tdev_warn(&pdev->dev, "no %s\\n", reg_names[i]);
\t\t\tcontinue;
\t\t}
\t\tpd->regs[i] = (struct ipa_reg){ m, args[0], args[1] };
\t}''', '''\tfor (i = 0; i < NR_REGS; i++) {
\t\tret = ipa_get_reg(np, reg_names[i], &pd->regs[i]);
\t\tif (ret)
\t\t\treturn dev_err_probe(&pdev->dev, ret, "missing %s\\n",
\t\t\t\t\t     reg_names[i]);
\t}
\tif ((pd->regs[STATE].mask & MASK_IPA_POWER_STATE) != MASK_IPA_POWER_STATE)
\t\treturn dev_err_probe(&pdev->dev, -EINVAL, "invalid IPA state mask\\n");''')
    replace('''\tif (IS_ERR(pd->dispc1) || IS_ERR(pd->anlg))
\t\treturn dev_err_probe(&pdev->dev, -ENODEV, "missing syscons\\n");''', '''\tif (IS_ERR(pd->dispc1))
\t\treturn dev_err_probe(&pdev->dev, PTR_ERR(pd->dispc1), "missing dispc1 syscon\\n");
\tif (IS_ERR(pd->anlg))
\t\treturn dev_err_probe(&pdev->dev, PTR_ERR(pd->anlg), "missing PLL syscon\\n");''')
    # A missing register must fail probe, rather than imitate a successful
    # hardware transition with the donor's fixed delay fallback.
    replace('''\tif (!r->rmap) {
\t\tusleep_range(1250, 5000);
\t\treturn 0;
\t}
''', '')
    data = text.encode('utf-8')
    if hashlib.sha256(data).hexdigest() != manifest['prepared_sha256']:
        raise ValueError('Prepared IPA source hash mismatch')
    return data


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _destination(kernel, name):
    path = kernel / name
    current = kernel
    for part in Path(name).parts:
        current = current / part
        if _is_link(current):
            raise ValueError('Refusing IPA destination link: ' + str(current))
        if not current.resolve().is_relative_to(kernel):
            raise ValueError('IPA destination escapes kernel root: ' + str(current))
        if current != path and current.exists() and not current.is_dir():
            raise ValueError('IPA destination parent is not a directory: ' + str(current))
    if path.exists() and not path.is_file():
        raise ValueError('IPA destination is not a regular file: ' + str(path))
    return path


def _append_plan(path, marker, addition):
    original = path.read_bytes() if path.exists() else b''
    text = original.decode('utf-8')
    if marker in text:
        if addition not in text:
            raise ValueError('Unexpected existing IPA build entry: ' + str(path))
        return original
    return original + ('\n' + addition + '\n').encode('utf-8')


def apply(kernel):
    kernel = Path(kernel)
    if _is_link(kernel):
        raise ValueError('Refusing linked IPA kernel root: ' + str(kernel))
    kernel = kernel.resolve(strict=True)
    if not kernel.is_dir():
        raise ValueError('IPA kernel root is not a directory: ' + str(kernel))
    data = source()
    target = _destination(kernel, TARGET)
    if target.exists() and target.read_bytes() != data:
        raise ValueError('Refusing to replace a foreign IPA source')
    entries = [
        ('drivers/pmdomain/Makefile', 'sprd/', 'obj-y += sprd/'),
        ('drivers/pmdomain/Kconfig', 'config SPRD_UMS9620_IPA_PD',
            'config SPRD_UMS9620_IPA_PD\n'
            '\tbool "Unisoc UMS9620 IPA power domain"\n'
            '\tdepends on ARCH_SPRD && PM && OF\n'
            '\tselect PM_GENERIC_DOMAINS\n'
            '\tselect MFD_SYSCON'),
        ('drivers/pmdomain/sprd/Makefile', 'ums9620-ipa-sys.o',
            'obj-$(CONFIG_SPRD_UMS9620_IPA_PD) += ums9620-ipa-sys.o'),
    ]
    planned = {target: data}
    for name, marker, addition in entries:
        path = _destination(kernel, name)
        planned[path] = _append_plan(path, marker, addition)
    # Validate every destination and existing entry before the first write.
    for path, content in planned.items():
        if not path.exists() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
