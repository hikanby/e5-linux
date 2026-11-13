# 天翼一号 2022 / TYH212U / ARES

这是面向原生 OpenWrt 移植的独立准备目录，目前推进到实验平台内核编译阶段。
**没有可刷入固件，没有验证过的替换内核，也尚未在手机上启动 OpenWrt。**

本适配以用户 Fork 的 `main` 提交
`74a7d577948492721692b321a51150de43f1be30` 为基线。
前期参考另核对了 kanoqwq/e5-linux 的提交
`8fcce2d53ed1f3cc2b2175143176038373a77307`；这里只移植本目录与受控入口的增量，
不导入该参考提交中其他机型的改动。保留原项目及子模块的来源和许可证。
其他机型的成功经验是参考，不是 TYH212U 的硬件支持结论。
用户的 Fork 为 `https://github.com/hikanby/e5-linux`，适配分支为
`feat/tyh212u-cloud`；不会替换 Fork 的 `main`。

## 已确认的设备资料

`profile.json` 只包含设备所有者原厂 Android 的脱敏观察值，不含备份、序列号、
IMEI、MAC、NV 或厂商固件。

| 项目 | 观察结果 |
| --- | --- |
| 系统 / 平台 | Android 11；ARES / ums9620_2h10 / ums9620；AArch64 |
| 原厂内核 | 4.14.199；gzip initramfs 可用，未启用 LZ4 initramfs / DEVTMPFS |
| 存储 | UFS；原厂 QogirN6Pro 相关配置 |
| 启动镜像 | Android boot header v2，2048 字节 page，内置 DTB，含 AVB 元数据 |
| Android 实际绑定 | sgm41511_chg、sc8551-charger、omnivision_tcm_spi |
| 显示 / 触摸 | 精确面板和触控芯片型号仍未确认 |

原厂属性与命令行的锁状态证据存在冲突。2026-11-14，设备所有者说明曾自行
刷入修改后的 boot，且知道恢复方法；这支持修改镜像曾被接受，但尚未验证
本实验内核的接受条件、具体恢复步骤及恢复结果。

UFS 原厂节点使用 `sprd,ufshc` 和打包的 `syscon-names`/`syscons`，没有标准
`resets` 属性；其时钟、校准 cell 与 `vdd-mphy` 名称和已有 QogirN6Pro 驱动相符。
公共 UFS 驱动现可解析这两种 syscon/reset 表达方式，`prepare-ufs.py` 是该组件的
固定基线入口。独立 ARM64 UFS 对象已通过云编译；复位寄存器行为仍待本机验证。

`prepare-kernel.py` 接入已核对来源和原厂绑定的 UMS9620 时钟、QogirN6Pro
引脚、UFS、IPA 电源域及实验 USB 组件。复用 `devices/m8/port/` 中固定摘要的 SoC 源文件，
不执行 M8 整机准备脚本，不导入其他机型的 DT 或充电策略。GPU 的两个时钟
provider 尚缺。USB PHY 作为独立模块编译；已移除参考机型的 PMIC DPDM 写入、
全局 PMIC 查找和 extcon 改写。DWC3 补充父节点 compatible，并检查 legacy
PHY 初始化错误、按成功引用退出；没有实现厂商板级状态机。
原厂双 extcon、BC1.2、OTG 供电和角色状态机尚未实现，不能据此启动 USB。
原厂 DT 同时启用 IPA DWC3 和 AON MUSB，后者为 peripheral。2026-11-14
只读观察确认当前 Android 到电脑的连接绑定 `25100000.dwc3`，已 configured，
速率为 high-speed；MUSB 已注册但 not attached。完整 mux 选择顺序仍未知。
实验 PHY 的 init 仍有未经本机验证的 IPA mux 序列，
且 PMIC EIC 的原厂 compatible 与基线驱动不匹配，不能直接加载测试 USB。
本机脱敏接线见 `usb-stock-bindings.json`，单次原厂运行观察见
`usb-stock-runtime.json`，来源对照与限制见
[USB-POLICY-EVIDENCE.zh-CN.md](USB-POLICY-EVIDENCE.zh-CN.md)。

核心启动的离线核对已找到 CPU/PSCI、GICv3、ARMv8 timer 和 UART fallback
的固定源码匹配，相关配置已启用。原厂静态 DT 与实际运行 DT 的内存和保留区
存在差异；实验 Image 的 text_offset 也不同于原厂。现装 A/B 两份启动器已与机主
提供的 PAC 完整核对摘要，其 Android 静态路径固定装载/跳转 `0x80080000`。
已据此评估在原生 Image 前增加填充和分支的入口方案；实际执行路径、临时内存
占用、最终 DTB 及 CPU 入口状态仍待验证。脱敏证据与边界见
[BOOT-EVIDENCE.zh-CN.md](BOOT-EVIDENCE.zh-CN.md)。
UART 尚无已验证的电脑接收路径。实验配置加入 pstore/ramoops 作为另一候选
日志能力，但跨重启保留、原厂系统读取和用户态日志接入均未验证。
独立 `diagnostic-minram-pmsg.cpio.gz` 候选补入 devtmpfs 挂载和用户态阶段标记，
仅供最小 RAM 内核后续集成；尚未在手机执行或验证保留效果。

## GitHub Actions 做什么

工作流为 `.github/workflows/tyh212u-diagnostics.yml`，名称
**TYH212U diagnostic foundation**。分支首次推送会触发它，前提是 Fork 已启用 Actions。
首次无需运行上游的 **Build and publish OpenWrt**；该发布流程不能代替本机型适配。

当前分为四类任务，其中内核任务按配置各编译一次：

1. 在 Ubuntu 24.04 上运行 boot-v2、initramfs、源码审计和受控构建入口的离线测试。
2. 从官方 SHA-256 固定的 BusyBox 1.37.0 源码交叉编译静态 AArch64 程序，生成
   `diagnostic.cpio.gz`。用 QEMU 验证 applet 和包内 init 的语法，不执行 init。
3. 检出固定 Linux 源码提交
   `020b970e351eee3887e6caaf2d653926e7086e44`，审计受审目录中的精确字段与配置符号。
   这是源码覆盖检查，不编译目标内核，不证明硬件可用。
4. 在同一固定提交上执行本目录的实验平台准备入口，分别生成平台编译配置和
   最小 RAM 诊断配置的 ARM64 `Image`。平台配置完成全部选中模块的链接；RAM
   配置禁用模块、存储、PMIC、regulator、USB 和网络，保留核心启动与日志能力。
   两者使用独立对象和产物目录，逐项检查配置、必要对象和摘要。独立入口工具
   同时检查每份 Image 的候选放置跨度，不生成包装组件。
   没有构建本机 DTB、boot 镜像或 OpenWrt 固件，也不证明硬件可工作。

工作流仅有 `contents: read` 权限，不需要用户密钥，不创建 Release、标签或部署。
它不连接手机。构建日志、产物摘要、BusyBox 对应源码及许可证会随 artifact 保存。
失败目录保留用于排查，不自动清理源码、对象或下载缓存。

在 Fork 的 **Actions** 中打开该名称的运行记录查看进度。成功后可下载
`tyh212u-ram-diagnostics-*`、`tyh212u-source-audit-*` 和
`tyh212u-experimental-platform-kernel-*`、`tyh212u-experimental-minram-kernel-*`，
但不能刷入这些文件。
新工作流若尚未出现在默认分支的手动运行列表，先使用本分支的 push 触发；
不要为了显示 Run workflow 而覆盖 main。详细构建入口见
[DIAGNOSTICS-BUILD.zh-CN.md](DIAGNOSTICS-BUILD.zh-CN.md)。
实验内核的固定输入、构建入口和产物说明见
[KERNEL-BUILD.zh-CN.md](KERNEL-BUILD.zh-CN.md)。

## 后续尚需完成

- 完成 USB PHY、角色切换、充电和供电策略的原厂证据核对与独立适配。
- 将实验平台配置补齐为可供本机启动的配置，核对原厂 DT、IRQ、定时器、
  保留内存、启动状态和 initramfs 依赖；实验内核通过编译不代表这些条件已满足。
- 单独解决 boot v2 集成、AVB 接受条件和可验证的恢复方式，才考虑受控的设备启动实验。
- 再接入 OpenWrt rootfs、网络、Wi-Fi、蜂窝、显示、触摸、充电等硬件，并逐项验证。

`tools/boot_v2.py` 仅提供离线检查、提取和字节一致的 roundtrip；对含 AVB 的原厂镜像
拒绝修改负载。它不是签名器，也不证明镜像可启动。`tools/diagnostic_initramfs.py`
仅生成 RAM 诊断档案；不生成 boot.img，不挂载 Android 分区或配置网络。

原厂 boot、DTB、完整配置、分区备份与设备标识只留在所有者本机，不加入公开仓库。
本目录不向生产机型列表或现有安装器注册 TYH212U，也不改变其他机型的安装流程。
