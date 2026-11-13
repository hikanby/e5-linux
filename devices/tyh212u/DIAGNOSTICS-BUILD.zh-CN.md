# TYH212U 云端 RAM 诊断组件构建

此入口只编译静态 AArch64 BusyBox 并生成 `diagnostic.cpio.gz`。没有内核、
boot.img、AVB 签名、OpenWrt rootfs 或刷机安装器；构建成功不代表手机可启动。
不连接设备，也不执行包中的 `/init`。

## 环境与入口

首个支持环境为 GitHub Actions `ubuntu-24.04` x86_64。依赖为 Python 3、
make、host gcc、`gcc-aarch64-linux-gnu`、`libc6-dev-arm64-cross`、
`binutils-aarch64-linux-gnu`、`qemu-user`、file、git；标准 Ubuntu runner
已有 host 编译工具，可显式安装 `build-essential`。BusyBox 官方源包是 bzip2
格式，Python `tarfile`/`bz2` 负责读取，CI 亦可安装 bzip2。

必须从仓库根目录经已有受控入口执行，明确登记本次工作区和新输出目录：

```sh
python3 tools/build-release.py run \
  --command-only --workspace "$RUNNER_TEMP/tyh212u-workspace" --label tyh212u-diagnostics -- \
  python3 devices/tyh212u/build-diagnostics.py \
    --workspace "$RUNNER_TEMP/tyh212u-workspace" \
    --out "$GITHUB_WORKSPACE/out/tyh212u-diagnostics"
```

不套用作者电脑的 `/home/kano/openwrt-build`。并行度固定为 2；不创建其他
release worker，不组装生产发布。已有输出目录或已准备的 source/object 树
会被拒绝，失败现场保留，不自动清理。固定目录为：

- 下载：`<workspace>/downloads/tyh212u-diagnostics/busybox-1.37.0.tar.bz2`；
  只有 SHA-256 与锁相同的完整文件可复用。
- 源码：`<workspace>/sources/tyh212u-diagnostics/busybox-1.37.0`。
- 对象：`<workspace>/objects/tyh212u-diagnostics/busybox-1.37.0`。

需要重跑时优先使用新 runner 的干净工作区；不要删除设备备份、历史缓存或
改写身份标记来强行复用失败目录。

## 来源与验证

`diagnostics-busybox.lock.json` 固定官方 BusyBox 1.37.0 源包和官方发布的
SHA-256 `3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4`。
构建时按该摘要验证完整源包，拒绝未登记 URL、路径穿越、链接或特殊 tar 成员。
不跟随最新版本，不执行远程安装脚本。

`allnoconfig` 生成完整默认 `.config` 后，构建器逐项覆盖受跟踪 seed 中的
配置项，再以 Python 子进程标准输入提供空行运行 `make oldconfig`，由 Kconfig
解算依赖，最后逐项核对实际 `.config`。整个过程有超时，不调用 shell 或 yes。
配置静态非 PIE AArch64 ET_EXEC、ash 和所需少量 applet。
mount 保留 `-o` 安全标志，关闭 fstab/mtab、mount helper、标签/UUID、CIFS、NFS。
以下不依赖 devtmpfs 的说明适用于默认 `console` 档案。
原厂 `CONFIG_DEVTMPFS` 未启用，包内 `/dev` 只含 initramfs 的 console/null
字符节点，不运行 mdev，不依赖 devtmpfs。

构建后进行 ELF 结构检查，并在 qemu-aarch64 用户态执行 `--list` 得到真实
applet 清单与二进制 SHA-256 绑定，再测试 printf、cat、readlink、`[`、sleep 0、
uname、ash 内建，以及 **mount --help**。mount 不实际挂载任何文件系统。
对最终包所含 init 仅执行 `sh -n` 语法检查，不执行 init，也不模拟硬件启动。

## 输出与界限

成功输出含 `diagnostic.cpio.gz`、`busybox.aarch64`、真实 applet manifest、
完整 BusyBox `.config`/seed、ELF 检查证据、`diagnostics-provenance.json`、
`ARTIFACT-BOUNDARY.json` 和 `SHA256SUMS`。附官方源包、源锁和原始 BusyBox
LICENSE，保留其许可证及对应来源。固定输入的 gzip/newc 序列化为确定性输出；
不宣称不同编译器版本生成的 BusyBox 二进制可逐字节重现。

init 只会将限定的 model/kernel/driver/UDC 信息写入 RAM/console，然后 sleep。
这些行为尚未在手机运行或验证。实际内核、驱动、启动链、AVB 和恢复条件仍需
独立适配；不得将本档或云端构建通过当成刷入或修改手机的授权。

另提供独立的 `diagnostic-minram-pmsg.cpio.gz` 和对应 init 源文件，复用同一个
已核验 BusyBox。它仅面向启用 devtmpfs/PSTORE_PMSG/ramoops 的最小 RAM 内核：
先显式挂载 devtmpfs，再检查 `/dev/pmsg0` 是字符设备，写入 init 到达、内核版本、
完成等待或停止原因的有限标记，每条以 `TYH212U-MINRAM-v1` 开头。
节点缺失或写失败会报告失败；不创建猜测设备号，不重试或自动重启。
它不采集完整命令行、设备标识或旧日志，也不打开 watchdog 或块设备。
该变体会写入供 pstore 使用的保留 RAM，跨重启保留和 Android 读取兼容性未验证；
早于用户态或 ramoops 初始化的失败仍可能没有标记。不能在 Android 上直接执行。
云端对包内脚本仅做语法检查；独立函数测试使用宿主伪终端和 `/dev/full` 验证
标记内容、缺失节点及写失败，均不执行 init，不访问手机。

仅变更诊断程序时，手动工作流可选 `scope=diagnostics`，只运行离线测试和
诊断组件构建，复用输入未变且已有来源校验的内核。默认 `all` 仍运行全部任务。

本机 Windows 可以运行离线逻辑测试，不能据此宣称交叉编译成功：

```sh
python -m unittest discover -s devices/tyh212u/tools -p 'test_*.py' -v
```
