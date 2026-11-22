# 更新日志

记录 2026-11-14 ~ 2026-11-22 围绕「深睡唤醒后页面回落 / 数据不更新」问题的修复，以及配套工具调整。按日期倒序。

## 2026-11-22

### 管理中心服务改为常驻
- `bridge/codex_bridge.py`：看门狗只锚定稳定的宿主进程（`VSCODE_PID` / `DOTII_HOST_PID`），不再扫描会自行重启的 IDE helper；宿主消失需连续 5 次检查（约 10 秒）才停服。新增 `--watch-pid`（追加监控）和 `--no-parent-watch`（关闭监控）。
- 新增 macOS LaunchAgent `com.dotii.bridge.dev`：登录自启 + 每 120 秒自检，服务不在才拉起，已在运行则不重复启动。守护脚本位于 `~/Library/Application Support/Dotii/start_bridge_daemon.sh`（不能放在 `~/Downloads`，launchd 会被 macOS 隐私保护拦截）。
- 背景：IDE 重启后旧宿主 PID 消失，看门狗停掉服务，设备离线后天气/拓竹一直显示缓存值。烧录脚本本身第一步就会停服务，不再依赖 IDE 生命周期管理。

## 2026-11-15

### 快照缓存：SPN3 → SPN4
- `main/app_state.c`：缓存 magic 从 `SPN2` 升级到 `SPN3`，加入自定义背景图标志与版本；随后升级到 `SPN4`，缓存结构由「逐字段挑选」改为直接保存完整 `display_snapshot_t`。
- 根因：旧缓存完全没有 Dotii 字段，且缺 `bambu_finish_at`、相机元数据等；深睡恢复时 `memset` 清零后 `dotii_enabled=false`，表情页面被判定禁用，拓竹显示残缺旧值。改为整体保存后不会再漏字段。
- 写入时清除易变字段（`generated_at`、在线标志）和相机帧信息（只存于 PSRAM，不持久化），恢复时只覆盖 `valid / preview_data / source_online / stale`。

### 自定义背景图持久化
- `main/connectivity.c`：434 KB 背景图写入 `storage` flash 分区（头部 `magic=0x444F5431` + revision + size + CRC32），开机加载；Wi-Fi 下载和 USB 分片传输两条路径都会落盘。
- 背景：图片原本只在 PSRAM，深睡等于重启必然丢失，自定义页回落为默认文字。
- `main/CMakeLists.txt`：新增 `esp_partition` 组件依赖。

### 整点报时覆盖深睡场景
- `main/board_input.c` / `main/main.c` / `main/audio_chime.*`：深睡前用 RTC 定时器排到下一个整点；静音时段（23:00–07:00，以及下一整点落在 23:00 的情况）直接排到 07:00。唤醒后播放报时，约 2.5 秒后继续深睡。
- 用 RTC 内存保存待报时的小时标记，配合 NVS 去重（`audio_chime/hour`、`audio_chime/job`），避免与醒着时的 LVGL 分钟 0 报时或重启重复播放。
- 静音时段宏统一放到 `main/audio_chime.h`，深睡调度与播放共用。

### 拓竹进度页手势
- `main/state_ui.c`：新增 `BAMBU_TAP_SLOP_PX=14`，`LV_EVENT_PRESSED` 记录按下点，释放时按位移阈值和 `LV_DIR_NONE` 过滤，避免短拖/斜拖误进详情页。

### 烧录脚本
- `flash_dotii.sh`：`APP_ADDR` 由错误的 `1048576 (0x100000)` 修正为 `65536 (0x10000)`，与 `verify-flash` 地址一致；原值会把固件写到错误偏移导致校验失败。

## 2026-11-14

### 首次深睡回落修复
- `main/state_ui.c`：增加 `display_config_trusted` 守卫，避免开机离线预览快照把 `clock_style` / `display_revision` 写回默认值。
- `main/app_state.c` / `main/connectivity.c`：新增 `ui_cache/snap` NVS 快照缓存（初版 `SPN1`/`SPN2`），离线时优先恢复上次真实数据而不是演示数据；用 `source_online` 而非 `preview_data` 判断快照是否来自管理中心。
- `main/audio_chime.c` / `main/main.c` / `main/CMakeLists.txt`：接入板载 ES8311 + I2S + 功放，独立 FreeRTOS 任务队列播放，实现整点报时和拓竹打印完成提示音；音频事件通过 NVS 跨重启去重。

### 验证
- `bridge/tests/test_module_on_demand.py`：新增看门狗宽限、多宿主 PID、自动发现等测试，共 23 项通过。
- 通过 NVS dump 确认 `SPN4` 缓存写入、`storage` 分区背景图头部正确；固件构建无警告，分块烧录 `FLASH_OK` + digest 校验通过。
