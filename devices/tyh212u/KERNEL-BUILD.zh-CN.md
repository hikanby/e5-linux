# TYH212U 实验平台内核编译

此入口检查平台驱动与 Linux 6.18.54 的编译和链接条件，产物不能刷入。
没有本机 DTB、boot 容器、OpenWrt rootfs、AVB 签名或安装器。
USB PHY、Type-C/充电策略、GPU 时钟及整机启动配置尚未适配。

## 固定输入与入口

基线为 `Enceka/linux-lts-e5` 提交
`020b970e351eee3887e6caaf2d653926e7086e44`。Actions 使用 Ubuntu 24.04，
安装 GCC AArch64 交叉编译器、make、bc、bison、flex、libssl-dev 和 libelf-dev。
源码检出须干净，随后由本目录 `prepare-kernel.py` 应用已校验来源的四个组件。
时钟、引脚、IPA 的来源摘要各在其 manifest 中；UFS 使用公共受跟踪源。

```sh
python3 tools/build-release.py run --command-only \
  --workspace "$RUNNER_TEMP/tyh212u-workspace" --label tyh212u-platform-kernel -- \
  python3 devices/tyh212u/build-kernel.py \
    --kernel "$GITHUB_WORKSPACE/ci/tyh212u-linux" \
    --workspace "$RUNNER_TEMP/tyh212u-workspace" \
    --out "$GITHUB_WORKSPACE/out/tyh212u-platform-kernel" --jobs 4
```

构建器验证工作区锁，使用固定对象目录
`<workspace>/objects/tyh212u-platform`，拒绝已有对象或产物目录。
不在 Windows 上交叉编译，不下载或复制原厂固件，不连接手机。
失败日志和解算后的配置由 Actions 单独保留。

`kernel.config` 是最小平台编译 seed，使用 `allnoconfig` 解算，然后逐项核对，
不能静默丢弃配置。执行 `make Image modules`，检查 ARM64 Image/ELF 头、
内核版本、四个必要平台对象及 `modules.order` 中每个对象的完整 `.ko`。
编译前后核对源码差异摘要，防止构建中输入变化。

## 产物与对应源码

成功 artifact 包含 `Image`、完整配置、System.map、Module.symvers、模块清单、
`modules.tar.gz`、`BUILD-RESULT.json`、`ARTIFACT-BOUNDARY.json` 和
`SHA256SUMS`。模块属于同次编译，归档保留安装目录层次，但未运行 depmod，
也未验证能在手机加载。`modules.order` 保留 Linux 6.18 的构建树 `.o` 格式。

`prepared-source-overlay.tar.gz` 保存所有修改与新增源码。其基线仓库、精确
提交、每个差异文件摘要、编译器版本及本适配仓库提交均在结果中登记。
在同一基线的全新目录应用该 overlay，可得到本次准备的源码；不要覆盖已有
准备树。随附基线 COPYING。seed 配置和构建器由结果中的 adapter commit 对应。

实验内核的成功结果只证明该配置的编译和链接通过。后续还须完成原厂 DT、
USB/供电策略、启动链和恢复条件，再考虑经过授权的设备启动实验。
