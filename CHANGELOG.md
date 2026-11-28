# 更新日志

记录 2026-11-14 ~ 2026-11-28 围绕「深睡唤醒后页面回落 / 数据不更新」问题的修复、提示音适配、语音播报、字体锯齿修复及 CI 自动发布。按日期倒序。

## 2026-11-28

### 修复时钟与温度字体锯齿及度数符号缺字
- 新增 `main/generated/ui_font_clock.c` / `ui_font_temp.c`：用 `lv_font_conv` 从 Geneva.ttf 预生成位图字体（120px / 48px，4bpp，`--no-compress` 保持 `bitmap_format = 0`，与项目现有字体格式一致）。时钟用 120px 预渲染字体替代 `montserrat_32` + `transform_scale` 放大，消除大数字锯齿；温度用 48px 预渲染字体替代 32px + 340% 缩放，字符集包含 `°`（U+00B0），避免度数符号显示为矩形。
- `main/state_ui.c`：移除时钟/温度的运行时 `transform_scale`，改用预渲染字体；`main/CMakeLists.txt` 将两个字体源文件加入编译。
- 实测：时钟数字清晰无锯齿，温度度数符号显示正常。

### 整点报时与打印完成改为中文语音播报
- 新增 `scripts/generate_voice_assets.py`：用 macOS `say -v Tingting` 生成 24 个整点（零点整～二十三点整）+「打印完成」，经 `afconvert` 转 16kHz/16bit 单声道 PCM，输出 C 数组到 `main/generated/voice_assets.c/.h`（25 段共 875KB）。重新生成只需在 macOS 跑该脚本。
- `main/audio_chime.c`：新增通用 PCM 播放 `voice_play()`，整点按当前小时播对应语音、打印完成播「打印完成」；移除旧的正弦音调生成代码。保留 +15dB 音量曲线、23:00–07:00 静音时段、NVS 去重、队列机制。
- `main/CMakeLists.txt`：语音资源 C 文件加入编译。固件 2.6MB → 3.6MB，app 分区仍有 31% 空闲。
- 实测：「打印完成」「十九点整」语音均可闻，Tingting 女声清晰。

## 2026-11-27

### GitHub Actions 自动构建与固件发布
- 新增 `.github/workflows/firmware-release.yml`：push 到 main 自动构建 ESP-IDF 6.0.2 固件并发布到 GitHub Release。版本号取 `1.2.<run_number>` 自动递增，CI 临时改写 `CMakeLists.txt` 的 `PROJECT_VER` 使固件内嵌版本与 Release 一致；组装标准 4 文件烧录包（flasher_args.json + 3 个 bin），校验清单偏移、文件齐全与内嵌版本后打 zip 发布。macOS 现有 build-check 逻辑不变。
- `.github/workflows/validate.yml`：删除对不存在的 `bridge/web/markdown.js` 的 `node --check`（CI 失败点）。
- `bridge/tests/test_firmware_dependencies.py`：`managed_components/` 是 ESP-IDF 从 `dependencies.lock` 生成且被 gitignore，干净 checkout 下不存在；原测试读 `CHECKSUMS.json` 直接失败。改为组件缺失时 skipTest，存在时仍校验 zlib 头哈希。
- 首次发布实测：`v1.2.2` / `dotii-firmware-1.2.2.zip`，两个 workflow 全绿。

## 2026-11-22

### 扬声器提示音适配 10mm 喇叭
- `main/audio_chime.c`：`CHIME_VOLUME_PERCENT` 65 → 100，打印完成音型从 880/1175Hz 改为全 1000Hz（150+150+500ms 三音节奏）。该 10mm 8Ω1W 喇叭规格 Fo=900Hz±20%、灵敏度 88dB，1kHz 落在共振峰内，偏离频率几乎听不到；靠节奏而非音高与整点报时（下行三音）区分。
- `main/audio_chime.c`：整点报时从 988/784/659Hz 改为 1046.5/880/784Hz（全落在共振区，659Hz 原本低于 Fo 下限听不到），时长 260/260/520ms → 450/450/900ms，保持下行三音与完成音节奏区分。
- 数字音量 100 仍偏小，追到根因：默认音量曲线把 100 映射为 0dB，扣掉 hw_gain(20·log10(3.3/5)≈−3.6dB) 后 ES8311 DAC 只开到 ~3.6dB（寄存器 ≈198/255）。用 `esp_codec_dev_set_vol_curve()` 把曲线上限提到 +15dB（加 hw_gain 补偿后寄存器 ≈228/255，仍在 +32dB 上限内），响度提升后两种提示音均可闻。
- 实测 +9dB → +15dB、正弦波 → 方波均无实质提升：5V PA 推 8Ω 已接近 1W 饱和，软件增益到顶。方波音色更刺耳，已退回正弦波；+15dB 保留为最终值。要再响只能改硬件（16–20mm 高灵敏度喇叭或提高 PA 供电）。
- 排障结论：音频硬件通道正常——开机无条件自检音（绕过快照/静音/去重）实测可闻。此前注入测试反复"没听到"的根因是测试脚本把设备时钟同步到 23:00–07:00 静音段，`chime_request()` 直接丢弃；改用白天时间注入后正常。
- 排查经验：USB 注入 epoch 会改写设备本地时间（UTC epoch + 本地时区），验证提示音时必须避开静音时段（本地 23:00–07:00），且整点测试要换未去重的 hour 键，否则误判为硬件故障。

### 触摸手势/点击灵敏度
- `main/state_ui.c`：LVGL 默认 `gesture_min_velocity=3`、`gesture_limit=50` 在 466px 圆屏 60Hz 刷新下过于严格——慢速滑动时每帧位移不足 3px 会把 `gesture_sum` 清零，导致手势永远不触发。运行时遍历 pointer indev，将阈值降为 `gesture_min_velocity=1`、`gesture_limit=24`，长按时间放宽到 500ms。
- `main/state_ui.c`：子控件默认不冒泡 `LV_EVENT_PRESSED` 到屏幕，`s_press_point` 常为旧值，导致顶部下滑/底部上滑的边缘判定失效。改为通过 `lv_indev_add_event_cb()` 在 indev 层捕获按下点。
- `main/state_ui.c`：`BAMBU_TAP_SLOP_PX` 从 14 放宽到 20，减少手指微动导致的点击漏判。

### 管理中心服务改为常驻
- `bridge/codex_bridge.py`：看门狗只锚定稳定的宿主进程（`VSCODE_PID` / `DOTII_HOST_PID`），不再扫描会自行重启的 IDE helper；宿主消失需连续 5 次检查（约 10 秒）才停服。新增 `--watch-pid`（追加监控）和 `--no-parent-watch`（关闭监控）。
- 新增 macOS LaunchAgent `com.dotii.bridge.dev`：登录自启 + 每 120 秒自检，服务不在才拉起，已在运行则不重复启动。守护脚本位于 `~/Library/Application Support/Dotii/start_bridge_daemon.sh`（不能放在 `~/Downloads`，launchd 会被 macOS 隐私保护拦截）。
- 背景：IDE 重启后旧宿主 PID 消失，看门狗停掉服务，设备离线后天气/拓竹一直显示缓存值。烧录脚本本身第一步就会停服务，不再依赖 IDE 生命周期管理。

### 拓竹打印完成提示音不响
- `main/state_ui.c`：`ui_timer` 原先把队列里的新快照直接收进全局 `s_snapshot`，导致 `update_snapshot()` 里 `previous_bambu_status` 读到的已经是新值，`COMPLETED` 边沿永远检测不到，完成提示音从未触发。改为收到独立临时变量后再更新 `s_snapshot`，保留旧值用于比对。
- 排障过程：开机自检音能响，确认 codec / I2S / 功放正常；通过 USB 注入 `completed` 快照复现；根因与静音时段、NVS 去重键无关。
- `main/audio_chime.c`：打印完成提示音改为「叮·叮——叮~」（880Hz 两短 + 停顿 + 1175Hz 长音），与整点报时（下行三音）和旧自检音（上行三音）明显区分。USB 注入完成事件实测可闻。
- 休眠补报：完成提示改为以 NVS job 键（文件名 + 完成时间戳）为唯一依据，不再只认 `PRINTING → COMPLETED` 边沿。断电重启 / 深睡唤醒期间完成的打印，在醒来后的首帧快照即可提示；同一任务重发不会重复播放。
- 整点报时实测：通过 USB 注入整点 epoch 触发 `ui_timer` 报时分支，确认醒着时整点报时可闻；深睡中的报时仍由 `schedule_hourly_wake()` 的 RTC 唤醒路径负责。

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
