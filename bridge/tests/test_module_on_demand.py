from __future__ import annotations

import sys
from dotii_test_support import temporary_directory
import unittest
import json
import plistlib
import re
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import codex_bridge  # noqa: E402
from runtime_paths import project_version  # noqa: E402
from platforms.macos import MacOSPlatformAdapter  # noqa: E402
from platforms.windows import WindowsPlatformAdapter  # noqa: E402
from bambu_client import BambuConfigStore, BambuService  # noqa: E402
from codex_bridge import (  # noqa: E402
    CUSTOM_DEFAULTS,
    resolve_ffmpeg,
    validate_custom_config,
    validate_module_config,
    validate_weather_config,
    watch_parent,
)


class ModuleOnDemandTests(unittest.TestCase):
    def test_management_only_packaging_can_reuse_the_formal_firmware_bundle(self):
        script = (BRIDGE.parent / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("$BuildFirmwareManifest", script)
        self.assertIn("$BundledFirmwareManifest", script)
        self.assertIn("$RefreshFirmwareBundle = $false", script)

    @unittest.skipUnless((BRIDGE.parent / "packaging/macos/Info.plist").is_file(), "macOS packaging is not included in this Windows checkout")
    def test_macos_info_plist_declares_bluetooth_usage(self):
        plist_path = BRIDGE.parent / "packaging" / "macos" / "Info.plist"
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)

        self.assertEqual(info["LSMinimumSystemVersion"], "13.5")
        self.assertIn("Dotii", info["NSBluetoothAlwaysUsageDescription"])
        self.assertIn("Dotii", info["NSLocalNetworkUsageDescription"])
        self.assertEqual(info["CFBundleShortVersionString"], project_version(BRIDGE.parent))
        self.assertEqual(info["CFBundleIconName"], "AppIcon")

    @unittest.skipUnless((BRIDGE.parent / "macos").is_dir(), "macOS host is not included in this Windows checkout")
    def test_macos_brand_icons_are_complete_and_used_by_native_host(self):
        root = BRIDGE.parent
        app_root = root / "macos" / "DotiiManagementCenter" / "DotiiManagementCenter"
        app_icon = app_root / "Assets.xcassets" / "AppIcon.appiconset"
        status_icon = app_root / "Assets.xcassets" / "StatusBarIcon.imageset"
        swift = (app_root / "main.swift").read_text(encoding="utf-8")

        for asset_set in (app_icon, status_icon):
            manifest = json.loads((asset_set / "Contents.json").read_text(encoding="utf-8"))
            for image in manifest["images"]:
                self.assertTrue((asset_set / image["filename"]).is_file())

        status_manifest = json.loads((status_icon / "Contents.json").read_text(encoding="utf-8"))
        self.assertEqual(status_manifest["properties"]["template-rendering-intent"], "template")
        self.assertIn('NSImage(named: "StatusBarIcon")', swift)
        self.assertIn("isTemplate = true", swift)

    @unittest.skipUnless((BRIDGE.parent / "macos").is_dir(), "macOS host is not included in this Windows checkout")
    def test_macos_native_host_and_packaging_sources_are_present(self):
        root = BRIDGE.parent
        swift = (root / "macos" / "DotiiManagementCenter" / "DotiiManagementCenter" / "main.swift").read_text(encoding="utf-8")
        packaging = (root / "packaging" / "macos" / "build_macos.sh").read_text(encoding="utf-8")

        self.assertIn("NSStatusBar.system.statusItem", swift)
        self.assertIn("SMAppService.mainApp", swift)
        self.assertIn('"--parent-pid"', swift)
        self.assertIn('"--host", "0.0.0.0"', swift)
        self.assertNotIn('"--host", "127.0.0.1"', swift)
        self.assertIn("PYINSTALLER_CONFIG_DIR", packaging)
        self.assertIn("codesign --verify", packaging)
        self.assertIn("notarytool submit", packaging)

    def test_parent_watchdog_stops_orphaned_backend(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False] * codex_bridge.HOST_WATCH_MISSES
        server = mock.Mock()

        with mock.patch.object(codex_bridge.os, "kill", side_effect=ProcessLookupError):
            watch_parent(43210, server, stop)

        server.shutdown.assert_called_once_with()

    def test_parent_watchdog_stops_when_any_host_disappears(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False] * codex_bridge.HOST_WATCH_MISSES
        server = mock.Mock()

        def kill(pid, _signal):
            if pid == 43211:
                raise ProcessLookupError

        with mock.patch.object(codex_bridge.os, "kill", side_effect=kill):
            watch_parent([43210, 43211], server, stop)

        server.shutdown.assert_called_once_with()

    def test_parent_watchdog_survives_host_restart(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False] * 8 + [True]
        server = mock.Mock()
        outcomes = iter([ProcessLookupError(), ProcessLookupError()] + [None] * 20)

        def kill(_pid, _signal):
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome

        with mock.patch.object(codex_bridge.os, "kill", side_effect=kill):
            watch_parent(43210, server, stop)

        server.shutdown.assert_not_called()

    def test_host_watch_pids_uses_stable_environment_anchors(self):
        environment = {"DOTII_HOST_PID": "200, 201", "VSCODE_PID": "102"}

        with mock.patch.dict(codex_bridge.os.environ, environment):
            self.assertEqual(codex_bridge.host_watch_pids(), [200, 201, 102])

    def test_development_backend_enables_host_watchdog_by_default(self):
        backend = (BRIDGE / "codex_bridge.py").read_text(encoding="utf-8")

        self.assertIn("watch_pids = host_watch_pids()", backend)
        self.assertIn('"--no-parent-watch"', backend)
        self.assertIn('"--watch-pid"', backend)

    def test_http_response_ignores_clients_that_disconnect_early(self):
        backend = (BRIDGE / "codex_bridge.py").read_text(encoding="utf-8")

        self.assertIn("BrokenPipeError, ConnectionAbortedError, ConnectionResetError", backend)

    def test_first_run_defaults_keep_weather_and_bambu_off(self):
        self.assertEqual(
            validate_module_config(None),
            {"weather": False, "bambu": False, "dotii": True},
        )

    def test_first_run_defaults_enable_custom_and_dotii_pages(self):
        self.assertTrue(CUSTOM_DEFAULTS["enabled"])
        self.assertTrue(validate_custom_config(None)["enabled"])
        self.assertTrue(validate_custom_config({})["enabled"])

    def test_weather_config_requires_coordinates_when_enabled(self):
        with self.assertRaises(ValueError):
            validate_weather_config({"enabled": True})
        validated = validate_weather_config({"enabled": True, "latitude": 31.23, "longitude": 121.47, "city": "上海"})
        self.assertEqual(validated["latitude"], 31.23)
        self.assertEqual(validated["city"], "上海")
        self.assertEqual(validate_weather_config(None)["enabled"], False)

    def test_frontend_overview_references_only_existing_status_elements(self):
        html = (BRIDGE / "web" / "index.html").read_text(encoding="utf-8")
        script = (BRIDGE / "web" / "app.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        refs = set()
        for pattern in (
            r'byId\("([^"]+)"\)',
            r'setText\("([^"]+)"',
        ):
            refs.update(re.findall(pattern, script))
        self.assertEqual(refs - ids, set())

    def test_dotii_disabled_content_is_hidden_even_with_grid_layout(self):
        styles = (BRIDGE / "web" / "styles.css").read_text(encoding="utf-8")
        script = (BRIDGE / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn(".dotii-content[hidden]", styles)
        self.assertIn('byId("dotii-content").hidden = !enabled;', script)

    def test_frontend_dotii_timed_state_returns_to_idle_without_poll_reset(self):
        script = (BRIDGE / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("state.dotiiLiveSignature !== signature", script)
        self.assertIn("state.dotiiLiveStartedAtMs = nowMs", script)
        self.assertIn('applyDotiiLiveDisplay("idle_breath"', script)
        self.assertIn("durationMs - elapsedMs", script)

    def test_dotii_animation_manifest_points_to_packaged_assets(self):
        manifest_path = BRIDGE / "web" / "assets" / "expressions" / "animations.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        animations = manifest["animations"]
        self.assertEqual(len(animations), 9)
        for animation in animations:
            asset = BRIDGE / "web" / animation["src"].lstrip("/")
            self.assertTrue(asset.is_file(), animation["id"])

    def test_bundled_ffmpeg_is_preferred_over_path(self):
        with temporary_directory() as temporary:
            folder = Path(temporary)
            bundled = folder / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()

            with (
                mock.patch.object(codex_bridge, "tools_root", return_value=folder / "tools"),
                mock.patch.object(codex_bridge, "current_platform", return_value=WindowsPlatformAdapter()),
                mock.patch.object(codex_bridge.shutil, "which") as which,
            ):
                resolved = resolve_ffmpeg(folder / "runtime")

            which.assert_not_called()
            self.assertEqual(resolved, str(bundled.resolve()))

    def test_macos_bundled_ffmpeg_is_preferred_without_path_lookup(self):
        with temporary_directory() as temporary:
            folder = Path(temporary)
            bundled = folder / "tools" / "ffmpeg" / "bin" / "ffmpeg"
            bundled.parent.mkdir(parents=True)
            bundled.touch()

            with (
                mock.patch.object(codex_bridge, "tools_root", return_value=folder / "tools"),
                mock.patch.object(codex_bridge, "current_platform", return_value=MacOSPlatformAdapter()),
                mock.patch.object(codex_bridge.shutil, "which") as which,
            ):
                resolved = resolve_ffmpeg(folder / "runtime")

            which.assert_not_called()
            self.assertEqual(resolved, str(bundled.resolve()))

    def test_bambu_snapshot_does_not_check_ffmpeg_until_rtsps_path_runs(self):
        with temporary_directory() as temporary:
            config = BambuConfigStore(Path(temporary) / "bambu.json")
            config.write(
                {
                    "host": "192.0.2.10",
                    "access_code": "12345678",
                    "serial": "01P00A000000000",
                    "camera_enabled": True,
                }
            )
            ffmpeg = mock.Mock(return_value=None)
            service = BambuService(config, lambda: True, ffmpeg)
            snapshot = service.snapshot()

            self.assertFalse(snapshot["connected"])
            self.assertFalse(snapshot["camera_available"])
            ffmpeg.assert_not_called()

    def test_disabling_bambu_closes_mqtt_and_camera_runtime(self):
        with temporary_directory() as temporary:
            config = BambuConfigStore(Path(temporary) / "bambu.json")
            config.write({"host": "192.0.2.10", "serial": "01P00A000000000", "access_code": "12345678"})
            enabled = {"value": True}
            service = BambuService(config, lambda: enabled["value"], lambda: None)
            mqtt = mock.Mock()
            camera_process = mock.Mock()
            camera_process.poll.return_value = None
            service._mqtt = mqtt
            service._camera_process = camera_process
            enabled["value"] = False

            service.set_enabled(False)

            mqtt.close.assert_called_once()
            camera_process.terminate.assert_called_once()

    def test_frontend_removes_dependency_panels_and_maintenance_actions(self):
        html = (BRIDGE / "web" / "index.html").read_text(encoding="utf-8")
        script = (BRIDGE / "web" / "app.js").read_text(encoding="utf-8")
        styles = (BRIDGE / "web" / "styles.css").read_text(encoding="utf-8")
        backend = (BRIDGE / "codex_bridge.py").read_text(encoding="utf-8")

        self.assertNotIn("Bambu 模块与相机依赖", html)
        self.assertNotIn("install-dependency", html + script)
        self.assertNotIn("reinstall-dependency", html + script)
        self.assertNotIn("/api/v1/admin/codex/check", backend)
        self.assertNotIn("/api/v1/admin/codex/check", script)
        self.assertIn("天气设置", html)
        self.assertIn("/api/v1/admin/weather/config", backend)
        self.assertIn("/api/v1/admin/weather/config", script)
        self.assertNotIn("/api/v1/admin/dependencies/install", backend)
        self.assertNotIn("/api/v1/admin/bambu/ffmpeg/install", backend)
        self.assertIn("保存并连接", html)
        self.assertIn("长按“重置配网”", html)
        self.assertIn("请长按 Dotii 右侧按钮进入设置", html)
        self.assertIn('class="bambu-help bluetooth-recovery-help"', html)
        self.assertIn(".bambu-help.bluetooth-recovery-help", styles)
        self.assertIn("border-top: 0", styles)
        self.assertNotIn("固件、显示与休眠设置不受影响", html)
        self.assertIn("相机画面暂不可用", html)
        self.assertIn("resolve_ffmpeg", backend)

    def test_bluetooth_setup_uses_reset_flow_instead_of_migration_token(self):
        html = (BRIDGE / "web" / "index.html").read_text(encoding="utf-8")
        script = (BRIDGE / "web" / "app.js").read_text(encoding="utf-8")
        backend = (BRIDGE / "codex_bridge.py").read_text(encoding="utf-8")

        self.assertNotIn('id="bluetooth-current-token"', html)
        self.assertNotIn("current_token: currentToken", script)
        self.assertNotIn('byId("bluetooth-current-token")', script)
        self.assertIn("长按“重置配网”", html)
        self.assertIn('current_token=payload.get("current_token", "")', backend)
        self.assertIn('connected ? t("已连接") : recognized ? t("已识别")', script)
        self.assertIn('blocked ? t("需要处理")', script)
        self.assertIn("已识别 Dotii ESP32-S3，可执行固件烧录", script)
        self.assertIn("登录系统后自动启动", html)
        self.assertNotIn("登录 Windows 后自动启动", html)



if __name__ == "__main__":
    unittest.main()
