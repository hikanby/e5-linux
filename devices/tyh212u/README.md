# 天翼一号 2022 / TYH212U / ARES

这是面向原生 OpenWrt 移植的独立准备目录，目前仍处于设备调查和诊断组件阶段。
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

已有 root 不等于 bootloader 会接受修改镜像。原厂属性与命令行的锁状态证据存在
冲突，不能据此声称启动链已开放。设备恢复路径尚未验证。

UFS 原厂节点使用 `sprd,ufshc` 和打包的 `syscon-names`/`syscons`，没有标准
`resets` 属性；其时钟、校准 cell 与 `vdd-mphy` 名称和已有 QogirN6Pro 驱动相符。
公共 UFS 驱动现可解析这两种 syscon/reset 表达方式，`prepare-ufs.py` 是该组件的
固定基线入口。它尚未在 TYH212U 内核树编译或启动，复位寄存器行为也仍待本机验证。

## GitHub Actions 做什么

工作流为 `.github/workflows/tyh212u-diagnostics.yml`，名称
**TYH212U diagnostic foundation**。分支首次推送会触发它，前提是 Fork 已启用 Actions。
首次无需运行上游的 **Build and publish OpenWrt**；该发布流程不能代替本机型适配。

当前分为四项：

1. 在 Ubuntu 24.04 上运行 boot-v2、initramfs、源码审计和受控构建入口的离线测试。
2. 从官方 SHA-256 固定的 BusyBox 1.37.0 源码交叉编译静态 AArch64 程序，生成
   `diagnostic.cpio.gz`。用 QEMU 验证 applet 和包内 init 的语法，不执行 init。
3. 检出固定 Linux 源码提交
   `020b970e351eee3887e6caaf2d653926e7086e44`，审计受审目录中的精确字段与配置符号。
   这是源码覆盖检查，不编译目标内核，不证明硬件可用。
4. 在同一固定提交上接入公共 QogirN6Pro UFS 驱动，检查最终配置符号为模块，
   交叉编译 ARM64 UFS host 目录并确认目标 `.o` 存在。它只检查 C 编译，不做模块
   完整链接，也不生成 TYH212U 的 Image、DTB 或 boot 镜像。

工作流仅有 `contents: read` 权限，不需要用户密钥，不创建 Release、标签或部署。
它不连接手机。构建日志、产物摘要、BusyBox 对应源码及许可证会随 artifact 保存。
失败目录保留用于排查，不自动清理源码、对象或下载缓存。

在 Fork 的 **Actions** 中打开该名称的运行记录查看进度。成功后可下载
`tyh212u-ram-diagnostics-*` 和 `tyh212u-source-audit-*`，但不能刷入这些文件。
新工作流若尚未出现在默认分支的手动运行列表，先使用本分支的 push 触发；
不要为了显示 Run workflow 而覆盖 main。详细构建入口见
[DIAGNOSTICS-BUILD.zh-CN.md](DIAGNOSTICS-BUILD.zh-CN.md)。

## 后续尚需完成

- 确认 ums9620 / QogirN6Pro 的时钟、中断、pinctrl、USB、供电和依赖，
  形成完整 TYH212U 专用配置与准备脚本；不套用其他机型的整套硬件补丁。
- 在 GitHub Actions 上编译 UFS 适配与完整内核，核对模块、原厂 DT 和 initramfs 依赖。
- 编译实际匹配的 Image / 完整模块，检查内核与原厂 DT、内存布局的兼容条件。
- 单独解决 boot v2 集成、AVB 接受条件和可验证的恢复方式，才考虑受控的设备启动实验。
- 再接入 OpenWrt rootfs、网络、Wi-Fi、蜂窝、显示、触摸、充电等硬件，并逐项验证。

`tools/boot_v2.py` 仅提供离线检查、提取和字节一致的 roundtrip；对含 AVB 的原厂镜像
拒绝修改负载。它不是签名器，也不证明镜像可启动。`tools/diagnostic_initramfs.py`
仅生成 RAM 诊断档案；不生成 boot.img，不挂载 Android 分区或配置网络。

原厂 boot、DTB、完整配置、分区备份与设备标识只留在所有者本机，不加入公开仓库。
本目录不向生产机型列表或现有安装器注册 TYH212U，也不改变其他机型的安装流程。
