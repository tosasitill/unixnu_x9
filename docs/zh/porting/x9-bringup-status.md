# OPPO Find X9（MT6993）UniXNU 启动进度

> 状态更新：2026-10-22。当前被测镜像已通过 LK 的内核解压、DTB overlay 和最终跳转准备，但设备仍在约 38.76 秒时发生 HWT。没有可确认的 BootShim/SEC 执行标记，因此尚不能说已进入 UEFI，更没有到达 DXE 或 UniXNU 页面。

## 结论摘要

- **目标：** 让 OPPO Find X9（PLJ110 / OP5E17L1，MT6993）启动 UniXNU 主页面；UEFI Shell 是否打包尚未确认，不能与 UniXNU 页面混为一谈。
- **当前真机候选：** `out/x9/boot/boot.img`，SHA-256 `2919883be46215e124f2ec001e13f6715623afce63e3470cf26b54217037b9be`，18,169,856 字节，含 16 MiB ramdisk。
- **已观察：** expdb 中该候选完成 LZ4 解压和 DTB overlay，输出 LK 最终跳转日志；之后记录到 WDT/HWT。
- **未观察：** BootShim 的明确执行见证、SEC/FV 解压、DxeCore、DXE、BDS、UniXNU 页面和 UEFI Shell。
- **当前粗略进度估算：** 约 25–30%，仅为里程碑估算，不是可量化完成度或 ETA。

## 最新候选的真机日志

本轮从设备取回的记录保存在 `/tmp/x9-audit/`：

- `expdb-current-tail.bin`
- `expdb-current-mid.bin`
- `expdb-current-head.bin`
- `pl_lk-current.txt`

候选对应的 expdb 记录包含以下关键内容：

```text
kernel_sz=0x00152756
64 bits kernel
dtb_size=0x0009F396
zimage_size=0x000b33c0
DT overlay ok, fdt size(4194304)
decompress_kernel takes 188 ms
booting linux @ 0x80000000, ramdisk @ 0xa3800000 (16777216)
  dtb @ 0x87c80000 (945423)
lk finished --> jump to linux kernel 64Bit
kedump: crc = 0x6197e63d
watch dog timeout, current timestamp = 38760795us
cpu(0) pc:<0000000038020128> lr:<0000000038020078>
```

`/proc/last_boot_reason` 为 `kernel crash HWT [2026-10-22 19:53:32]`。`/sys/fs/pstore` 没有 console ramoops 记录；目录中只有 `pmsg-ramoops-0`。当前收集到的 expdb/pstore 中没有 `X9SHIM` 标记。

**证据边界：** `:880/:883` 证明 LK 到达最终跳转准备并打印跳转信息，不单独证明 CPU 已在 shim 入口执行。WDT 捕获时 PC/LR 位于 LK 地址范围；这不能证明此前绝无 shim 执行，也不能确认 shim 已运行。当前没有 SEC 阶段的正向标记。

`/proc/pl_lk` 保存的是随后进入 Android 的启动日志，不应把其中 stock Android 的启动参数与失败候选混为一谈；本次候选判断以对应 expdb 记录和 `last_boot_reason` 为准。

## 候选镜像的离线核验

本地镜像 SHA-256 与 `out/x9/boot/boot.json` 中的 manifest 一致。当前镜像的 LK `kernel_sz` 与镜像记录吻合，ramdisk 为 16 MiB。LZ4 数据离线解压为 6 块、总计 42,101,248 字节；DTB 紧随 LZ4 帧。解压后的 MZ 头字段已按 stock 数据核对：`+8=0`、`+0x10=0x028e0000`、flags=`0xa`、`+0x3c=0x40`。入口 trampoline 的反汇编目标为 `_Start`，偏移 `0x18ec`。

这些检查确认候选文件的构造与打包，不代表对 MZ 头的 LK/ATF 解释已有独立的设备端证明。最新硬件日志说明 LK 已完成解压/overlay 并到达跳转准备，但仍没有证明 shim/SEC 执行。

## 已解决或已被新证据收窄的问题

1. **旧候选的零 ramdisk 问题：** 较早的被测镜像在 LK `mt_boot.c:950` 触发 `ramdisk_target_addr != 0` 断言，随后 HALT 并发起 BL33 crash reset。X9 的 `--ramdisk none` 现会打包 124 字节的有效空 newc，避免零长度 ramdisk 生成零加载地址。
2. **旧 LZ4/DTB 失败：** expdb 中更早的记录出现过 `unlz4 error: decodes failure`。当前候选日志已明确显示解压成功、overlay 成功；不应把旧失败当作本轮失败点。
3. **当前阻塞：** 最新镜像越过上述 LK 处理步骤后仍发生 HWT；最早的未确认阶段是最终交接及 BootShim/SEC 入口，不是已观察到的 UniXNU 或 XNU 页面问题。

旧记录与当前候选并无镜像 digest 的密码学绑定。只在记录有对应参数/构建证据时将其归属于特定候选。

## 硬件与显示约束

以下是测量或采集值，不是推断：

| 项目 | 值 |
| --- | --- |
| SoC | MT6993，8 CPU，约 12 GiB RAM |
| UFS HC / PHY | `0x16810000` / `0x16800000` |
| 面板 | `panel_ac287_p_7_a0020_dsi_cmd`，PanelID `0x00950601` |
| 显示模式 | 1256×2760，pitch 5120，command mode，DSC v1.2、SPR |
| DSI | `0x3e730000`，4 lanes，1106 Mbps/lane |
| UART | 无可用线缆 |

面板是 command mode；写入 bootloader 线性 framebuffer 不会自动让面板显示新帧。因此黑屏/没有色块不能定位 SEC 或 DXE 阶段。当前 minimal 配置也不能作为完整 UFS/DSI 真机验证结果。

## 启动阶段状态

| 阶段 | 当前证据 | 状态 |
| --- | --- | --- |
| LK 读取 boot image | 当前候选有对应 expdb 记录 | 已观察 |
| LZ4 解压、DTB overlay | 当前候选有成功日志 | 已观察 |
| LK 最终跳转准备 | 有 `:880/:883` 相关日志 | 已观察，不等于已执行目标入口 |
| BootShim / SEC | 无明确 marker | 未确认 |
| 内层 FV / DxeCore / DXE / BDS | 无阶段日志 | 未验证 |
| UniXNU 页面 | 未到达 | 未验证 |
| UEFI Shell | 固件中是否包含 Shell 应用尚未检查 | 未确认 |
| UFS、DSI 和 XNU | 尚未达到可验证阶段 | 未验证 |

## Host-side verification

The UniXNU package smoke suite builds the UEFI application and runs its QEMU `virt` cases. The latest run passed the menu/default path, keyboard selection, no-GOP fallback, GPT/FAT block-slice handoff, and kext protocol positive/negative cases. This verifies package behavior in QEMU only; QEMU does not emulate MT6993 UFS, DSI or the X9 boot chain and is not X9 hardware evidence.

## 下一步

1. 以当前 SHA 对应的日志作为基线，不重复追查已经在当前记录中成功的 LK 解压与 overlay。
2. 优先建立能在无 UART 条件下区分“最终交接已发生”和“仍在 LK/ATF”的可靠阶段见证，再定位 HWT 前的最早失败点。
3. 若得到 BootShim/SEC 正向证据，再逐段验证 SEC、FV 解压、DxeCore、DXE/BDS，最后才验证 GOP/显示和 UniXNU 页面。
4. 若目标还包括 UEFI Shell，单独检查 FV/FDF 是否含 Shell 应用，并作为独立里程碑记录。

## 复现与产物

- 候选镜像：`out/x9/boot/boot.img`
- Manifest：`out/x9/boot/boot.json`
- 本机拉取的 expdb/pstore 文件：`/tmp/x9-audit/`
- 本状态文档：`docs/zh/porting/x9-bringup-status.md`

本轮真机结果是 LK 通过解压/overlay/跳转准备，之后 HWT；它不是 SEC、DXE、UniXNU 或 Shell 成功结果。
