# TYH212U USB、充电与角色策略证据

记录日期：2026-11-14。当前结论服务于实验内核的编译准备，尚未验证
替换内核中的 USB 枚举、Type-C 角色、普通充电、快充或 OTG。
本次分析未连接手机，也未发布原始 DT、固件或设备标识。

## 证据来源和适用范围

| 来源 | 固定版本 | 可支持的结论 |
| --- | --- | --- |
| 本机原厂 DT 的选定脱敏属性 | [usb-stock-bindings.json](usb-stock-bindings.json) | 本机节点、属性和引用关系；不能证明当前驱动绑定、物理器件装配或运行时行为 |
| Realme AndroidT 供应商参考树 | `realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source`，`4381465ccaf87fcf3215b9cd42f4a685df40e5e0` | 相似平台的参考实现；不是 TYH212U Android 原厂源码 |
| 实验内核基线 | `Enceka/linux-lts-e5`，`020b970e351eee3887e6caaf2d653926e7086e44`，Linux 6.18.54 | 本次实际准备目标中的接口、匹配和角色处理路径 |
| M8 缓存 PHY | [port/usb-phy-sources.json](port/usb-phy-sources.json) 中的固定来源及原始文件摘要 | 借鉴的代码输入及附加策略；不能代表本机原厂策略 |

Realme 参考入口：
[固定提交](https://github.com/realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source/tree/4381465ccaf87fcf3215b9cd42f4a685df40e5e0)。
下文的行号均对应固定来源的原始文件，不随准备后的代码行数变化。

## 本机 DT 已确认的关系

- IPA DWC3 `/soc/ipa-apb/usb3@25100000`、AON MUSB
  `/soc/aon/usb@a00000` 及各自 PHY 同时为 `okay`。AON MUSB 的
  compatible 是 `sprd,qogirn6pro-musb`，厂商 `dr-mode` 属性为
  `peripheral`；它引用 AON USB2 PHY，并与 DWC3 路径共享 VBUS/Type-C
  extcon 和供电引用。此状态不说明哪一个控制器已绑定驱动或正在充当 UDC。
- DWC3 父节点是 `sprd,qogirn6pro-dwc3`；`extcon[0]` 引用 GPIO VBUS，
  `extcon[1]` 引用 UMP96xx Type-C。
- 父节点及 `snps,dwc3` core 子节点各自的 `usb-phy[0]`、`usb-phy[1]`
  都引用同一个 `sprd,qogirn6pro-ssphy`。core 已使用标准 compatible，
  不需要其他机型的 `snps,sprd-dwc3` 别名。
- PHY 的 extcon 仅引用 GPIO VBUS。该 GPIO 是 PMIC EIC 的 line 0，
  节点没有 `id-gpios`，不能单独提供 Type-C 主从角色或充电器类型。
- DWC3 的 `vbus-supply` 引用 `/charger-manager/cm_otg_vbus`，名称为
  `vddvbus`。该子节点未直接描述寄存器或 enable GPIO，实际 provider
  和调用链仍待核对，不能把它直接认作 SGM 驱动提供的 `otg-vbus`。
- DT 包含 `Sgm,sgm41511_chg`、Type-C、PD、charge pump 及 charger-manager
  的引用。manager 还列有备用供电名称；名称和 enabled 节点不足以证明
  所有列举芯片均已装配或驱动均已注册。
- 本机 PMIC syscon 的三个 `reg` cell 为 `0x2000`、`0x23a0`、`0x2398`。
  它们与参考代码读取位置相符，只构成映射线索，不证明本机寄存器含义、
  DPDM 所有权或互斥时序已经确认。

完整的脱敏引用关系及尚缺项目保存在 `usb-stock-bindings.json`。

## 双控制器与 mux 边界

原厂 DT 同时启用 AON MUSB 与 IPA DWC3，不能仅凭节点状态确定物理 USB
mux 选择、实际驱动绑定或活动 UDC。需要本机运行时绑定和原厂控制器切换
证据，再决定应适配哪条调试通道，不能把强制选择 IPA 当作已核对的板级策略。

M8 缓存 PHY 在 probe 和 init 都包含选择 IPA_SYS 控制器的寄存器写入。
当前 `usb-phy.py` 删除了 probe 中的独立写入，但仍保留 init 序列中的
`REG_AON_APB_AON_SOC_USB_CTRL` / `MASK_AON_APB_USB20_CTRL_MUX_REG`
操作，并为其增加错误传播。该序列对应原缓存 339–343 行；准备后的
`sprd_ssphy_init()` 仍会选择 IPA mux。保留初始化顺序只为组件编译研究，
未经本机验证，也未实现与 AON MUSB、Type-C 或厂商互斥状态机的协调。
因此“probe 不再写 mux”不能解释为“此 PHY 已无控制器选择策略”。

## VBUS 的 EIC provider 匹配缺口

本机 VBUS GPIO provider 使用 `sprd,ump96xx-eic`、`sprd,sc27xx-eic`。
固定基线
[`gpio-pmic-eic-sprd.c`](https://github.com/Enceka/linux-lts-e5/blob/020b970e351eee3887e6caaf2d653926e7086e44/drivers/gpio/gpio-pmic-eic-sprd.c)
的匹配表只有 `sprd,sc2731-eic`（365 行）；同固定 Realme 参考文件也只
匹配该 compatible（369 行）。两个文件中的 SC2731 寄存器、debounce
及中断类型处理相似，不能据此证明 UMP9620 EIC 与其兼容。

基线 GPIO extcon 能解析本机旧式 `vbus-gpio` 属性，仅说明 consumer
属性可识别。EIC provider 未匹配时，整条 VBUS 检测链仍不成立。
本次不猜测或加入 UMP96xx/sc27xx alias；须取得实际 UMP9620 EIC
实现、寄存器资料或等价的本机证据后再确定准备方案。详细对照和摘要在
`usb-stock-bindings.json` 的 `fixed_baseline_source_review` 中。

## 参考 PHY 与 BC1.2

Realme 的
[`phy-sprd-usb31-qogirn6pro.c`](https://github.com/realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source/blob/4381465ccaf87fcf3215b9cd42f4a685df40e5e0/drivers/usb/phy/phy-sprd-usb31-qogirn6pro.c)
在 688–733 行使用 kthread worker 处理 VBUS 与 BC1.2 通知，759–765 行的
charger callback 委托 `sprd_bc1p2_charger_detect()`。842–855 行按 compatible
全局查找首个 `sprd,ump962x-syscon`，再使用其父设备 regmap；这不是明确的
PMIC phandle，不能据此确认选中了本机正确的 PMIC 实例。

此固定文件及同树的 `phy-sprd-qogirn6pro.c` 没有 `dpdm`、`DPDM`、
`CHGR_DET` 或 `switch_to_phy` 文本匹配。此项只说明两个已核对源文件没有
直接实现该名字的切换，不排除其他组件控制 DP/DM。

独立的
[`sprd-ump96xx-bc1p2.c`](https://github.com/realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source/blob/4381465ccaf87fcf3215b9cd42f4a685df40e5e0/drivers/usb/phy/sprd-ump96xx-bc1p2.c)
使用全局 BC1.2 实例。178–210 行负责加锁、初次检测与重检测；212–237 行
更新 charger 类型并通知 PHY 消费者。probe 在 249、256、263 行分别从
本节点 `reg[0..2]` 取得 `charge_status`、`chg_det_fgu_ctrl`、
`chg_bc1p2_ctrl2`，271 行使用本节点父设备 regmap，282 行匹配
`sprd,ump9620-bc1p2`。因此此参考实现的检测地址来自 DT，并非固定写入
`0x23a0`。

M8 缓存源的 `sc27xx_dpdm_switch_to_phy()` 位于 136–160 行，使用固定
`CHGR_DET_FGU_CTRL = 0x23a0`、bit 14 和 bit 0；816 行在 probe 中无条件
调用该函数。原文件 SHA-256 为
`0d25e915d7e4be8e0ef605f5b6c6dd2ed142f9a7f9d23eaa04916d06f0f898a4`。
这是 M8 缓存的附加策略，不能作为已确认的 TYH212U 原行为。
当前 [usb-phy.py](usb-phy.py) 明确移除该 PMIC 发现和寄存器写入路径，
并保留本机 extcon 拓扑；这只建立 PHY 编译条件，充电和角色策略仍缺失。

## 参考充电器的 OTG 顺序

Realme 的
[`sgm41511-charger.c`](https://github.com/realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source/blob/4381465ccaf87fcf3215b9cd42f4a685df40e5e0/drivers/power/supply/sgm41511-charger.c)
在 1029–1069 行先设置 PMIC `BIT_DP_DM_BC_ENB` 禁止 BC1.2，再写 SGM
`REG01_OTG_ENABLE`，并安排 watchdog 与 OTG work；SGM 写入失败时恢复
BC1.2 位。1072–1100 行的关闭路径先关闭 SGM OTG，再恢复 BC1.2 检测。

1138–1145 行描述固定 5 V 的 `otg-vbus` regulator，1309–1314 行只在
master role 注册。1222–1266 行仍按 compatible 全局寻找 PMIC syscon，
从其 `reg[1]`、`reg[2]` 取得 charger detect/powerdown 地址，再取 syscon
父设备 regmap。上述顺序属于此供应商版本，不能直接填入本机的
`cm_otg_vbus` provider 或把本机原始 `role` 数字解释成已验证的运行状态。

同固定树的 `charger-manager.c` 没有不区分大小写的 `otg` 文本匹配，
因此本次参考文件不能解释本机 `cm_otg_vbus` 的实际实现。

## 厂商 DWC3 glue 与通用 glue 的差距

Realme 的
[`dwc3-sprd.c`](https://github.com/realme-kernel-opensource/realme_C51_C53_Narzo-N53-AndroidT-kernel-source/blob/4381465ccaf87fcf3215b9cd42f4a685df40e5e0/drivers/usb/dwc3/dwc3-sprd.c)
承担板级策略：

- 454–496 行先调用 PHY charger callback，仅 SDP/CDP 安排数据连接；
  UNKNOWN 等其他类型进入充电路径。
- 526–561 行将设备模式与 PHY VBUS、runtime PM、connect/disconnect
  通知及 USB role 协调。
- 573–623 行将主机模式与 VBUS regulator、PHY VBUS、runtime PM 及
  USB role 协调。
- 1213–1220 行读取父节点两个 legacy PHY；1341–1369 行分别监听父
  `extcon[0]` 的 `EXTCON_USB` 和 `extcon[1]` 的 `EXTCON_USB_HOST`，
  缺少第二个 provider 时才回退到第一个。

该文件还有 `sprd_usbm`、USB 音频和 MUSB 互斥及厂商状态机依赖，不能只
复制整个 glue 并以编译通过宣称完成适配。

基线
[`dwc3-of-simple.c`](https://github.com/Enceka/linux-lts-e5/blob/020b970e351eee3887e6caaf2d653926e7086e44/drivers/usb/dwc3/dwc3-of-simple.c)
只提供父节点时钟、可选 reset、子节点创建和基础 PM。
[dwc3.py](dwc3.py) 加入本机 compatible 后，仍不处理上述双 extcon、
BC1.2 分类、boost regulator 或 Type-C 协调。

基线
[`core.c`](https://github.com/Enceka/linux-lts-e5/blob/020b970e351eee3887e6caaf2d653926e7086e44/drivers/usb/dwc3/core.c)
在 1564–1565 行读取 core 自己的 `usb-phy[0..1]`；2008–2009 行只读取
自己的 `extcon[0]`，另有自身 role-switch 或 PHY graph 路径。基线
[`drd.c`](https://github.com/Enceka/linux-lts-e5/blob/020b970e351eee3887e6caaf2d653926e7086e44/drivers/usb/dwc3/drd.c)
在 545–551 行选择 role-switch，或从同一个 extcon 订阅
`EXTCON_USB_HOST`。这些路径不会自动消费父节点的双 extcon 策略。

## 初始化错误与回滚验证边界

固定基线 `core.c` 的 `dwc3_phy_init()` 在 836–837 行先后调用
`usb_phy_init(dwc->usb2_phy)`、`usb_phy_init(dwc->usb3_phy)`，没有检查
这两次 legacy PHY 返回值。因此当前 PHY callback 会返回寄存器错误或
超时，不等于 DWC3 会中止 probe。本机两个 legacy PHY 引用还指向同一个
对象，硬件实验前须适配 core 的错误处理，并核对部分初始化失败、两个
引用及 shutdown/unwind 的配对语义。当前编译准备未修改该 core 行为。

PHY 失败路径的 `sprd_ssphy_power_off()` 只关闭其实现中列明的部分状态，
不恢复 init 改动的全部时钟或控制器 mux，也没有证明可以恢复原厂其他
组件的寄存器所有权。离线故障注入能验证 callback 返回值和该 regulator
consumer 的 supply 引用平衡，不能称为完整硬件回滚，更不能替代本机
原厂顺序与替换内核运行结果的对照。

## 进入硬件验证前还缺什么

1. 确认本机 AON MUSB/IPA DWC3 的实际驱动、活动 UDC、mux 选择和
   控制器互斥时序，再确定最小调试通道。
2. 找到本机 `cm_otg_vbus` 的注册者、实际 boost 驱动和失败回退路径。
3. 依据本机驱动绑定及原厂观察，确认 DPDM、BC1.2、PD/快充各自的
   所有者、PMIC 实例和互斥顺序。
4. 取得 UMP9620 EIC 的适配证据，核对 Type-C/PD、充电器、PHY 与
   控制器在基线中的绑定和依赖条件，不能只增加 compatible。
5. 适配 legacy PHY 初始化失败的 core 处理、同 PHY 两引用的 unwind，
   并根据已确认的寄存器所有权补齐失败恢复。
6. 再确定设备模式调试通道所需的供电和角色实现，记录原厂与
   替换内核在插拔、枚举、普通充电及 OTG 切换时的可对照证据。

目前没有足够的硬件样本或启动结果给出可靠成功百分比。编译和链接
成功可证明代码兼容性取得进展，不能推导 USB 可用或整机 OpenWrt 成功。

## 本次参考文件 SHA-256

以下 Realme 文件均来自上述固定提交，只取这些选定文件作研究，未将
整套供应商树作为本机源码或生产构建输入。

| 原仓库路径 | SHA-256 |
| --- | --- |
| `drivers/usb/phy/phy-sprd-usb31-qogirn6pro.c` | `dcae8348f5fc4956ae40d7ed6acd8d06b42f1212b567c1d6eeffe8f816dec2b2` |
| `drivers/usb/phy/phy-sprd-qogirn6pro.c` | `03f398d8700d46f429063ab5d8eb13ad4313d6d2c238e9f7d8ffcb5a996f7cd3` |
| `drivers/usb/phy/sprd-ump96xx-bc1p2.c` | `caa7db02ad5ffb697336b8a5febbf200e8a2c7849d5fc729381b9673f0c229d1` |
| `drivers/usb/dwc3/dwc3-sprd.c` | `4241fa1e0c666d30569ed9ef5c12840e4f7ab52bd0d13ef0bd01838e6b5ebe84` |
| `drivers/power/supply/sgm41511-charger.c` | `c7b3b892fe1be81545522d5fc7ac8473a87fa2d2706991b008ba47106c7f43b2` |
| `drivers/usb/phy/phy.c` | `cdc26b3f2b1b1313150691a59455c4419170ee80d276a8913fd00b89b656f8aa` |
| `drivers/power/supply/charger-manager.c` | `82517b2d856985b650b597e7380d0d2f08c0a801ff8093ac0b0ba0bd6ee31918` |
| `include/linux/power/sprd-ump96xx-bc1p2.h` | `077492d3b2592b08d5ace457a39186b3106d1fc7bc692e6f450f1e9b706e71f9` |
| `drivers/power/supply/sc27xx_fast_charger.c` | `0e4167f18a725be705d77f01dbfff45e5b720a3cadf76129293eb6ef5cc474d7` |
| `drivers/gpio/gpio-pmic-eic-sprd.c` | `b9ab2ad7f8085caf90fef6d08c3c256c52157fa244aef5e63ddaf864e76e203d` |
