from __future__ import annotations

import sys
from dotii_test_support import temporary_directory
import unittest
from pathlib import Path
from unittest import mock


BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from codex_bridge import (  # noqa: E402
    default_dotii_config,
    dotii_state,
    lunar_date_text,
    packaged_startup_command,
    startup_command,
    validate_display_config,
    validate_dotii_config,
    validate_module_config,
    validate_snapshot,
    validate_weather_config,
    validate_weather_status,
)
from platforms.windows import WindowsPlatformAdapter  # noqa: E402


class SnapshotConfigTests(unittest.TestCase):
    def test_module_config_migrates_legacy_codex_module_to_weather(self) -> None:
        modules = validate_module_config({"codex": True, "bambu": False})
        self.assertEqual(modules, {"weather": False, "bambu": False, "dotii": True})

    def test_snapshot_defaults_to_safe_date_and_weather(self) -> None:
        snapshot = validate_snapshot({"schema_version": 1})
        self.assertEqual(snapshot["date"]["lunar_text"], "")
        self.assertFalse(snapshot["weather"]["available"])
        self.assertFalse(snapshot["weather"]["enabled"])
        self.assertEqual(snapshot["weather"]["condition_code"], 0)

    def test_snapshot_ignores_legacy_codex_section_and_rejects_bad_schema(self) -> None:
        snapshot = validate_snapshot({"schema_version": 1, "codex": {"task": {}}})
        self.assertNotIn("codex", snapshot)
        with self.assertRaises(ValueError):
            validate_snapshot({"schema_version": 2})

    def test_snapshot_bounds_lunar_text(self) -> None:
        long_text = "农历" * 100
        snapshot = validate_snapshot({"schema_version": 1, "date": {"lunar_text": long_text}})
        self.assertLessEqual(len(snapshot["date"]["lunar_text"].encode("utf-8")), 64)

    def test_snapshot_bounds_weather_values(self) -> None:
        snapshot = validate_snapshot({
            "schema_version": 1,
            "weather": {
                "enabled": True,
                "available": True,
                "city": "上海",
                "condition_code": 61,
                "temperature_c": 23,
                "temp_low_c": 18,
                "temp_high_c": 28,
                "humidity_percent": 65,
                "wind_kph": 12,
            },
        })
        weather = snapshot["weather"]
        self.assertTrue(weather["available"])
        self.assertEqual(weather["condition_code"], 61)
        self.assertEqual(weather["temperature_c"], 23)
        self.assertEqual(weather["temp_low_c"], 18)
        self.assertEqual(weather["temp_high_c"], 28)
        self.assertEqual(weather["humidity_percent"], 65)
        self.assertEqual(weather["wind_kph"], 12)

    def test_weather_status_rejects_out_of_range_values(self) -> None:
        with self.assertRaises(ValueError):
            validate_weather_status({"available": True, "temperature_c": 999})
        with self.assertRaises(ValueError):
            validate_weather_status({"available": True, "humidity_percent": 120})
        with self.assertRaises(ValueError):
            validate_weather_config({"latitude": "31.2"})

    def test_lunar_date_text_contains_ganzhi_and_lunar_fields(self) -> None:
        text = lunar_date_text(1794307200)  # 2026-11-14 local time
        self.assertIn("丙午", text)
        self.assertIn("农历", text)

    def test_dotii_state_reports_bambu_without_codex(self) -> None:
        idle = dotii_state({}, True)
        printing = dotii_state({"configured": True, "connected": True, "status": "printing"}, True)
        completed = dotii_state({"configured": True, "connected": True, "status": "completed"}, True)
        fault = dotii_state({"configured": True, "connected": True, "status": "fault"}, True)
        paused = dotii_state({"configured": True, "connected": True, "status": "paused"}, True)

        self.assertEqual(idle["state"], "idle")
        self.assertEqual(printing["state"], "bambu_printing")
        self.assertEqual(printing["expression"], "working")
        self.assertEqual(completed["expression"], "complete")
        self.assertEqual(fault["expression"], "failure")
        self.assertEqual(paused["expression"], "curious")

    def test_dotii_config_allows_one_animation_to_cover_multiple_states(self) -> None:
        config = default_dotii_config()
        config["animations"]["curious"]["states"].remove("bambu_paused")
        config["animations"]["working"]["states"].append("bambu_paused")
        config["animations"]["working"]["state_duration_ms"] = 3000
        validated = validate_dotii_config(config)
        state = dotii_state(
            {"configured": True, "connected": True, "status": "paused"}, True, validated,
        )
        self.assertEqual(state["expression"], "working")
        self.assertEqual(state["state_duration_ms"], 3000)
        self.assertFalse(state["state_hold"])
        self.assertEqual(
            validated["animations"]["working"]["states"],
            ["bambu_printing", "bambu_paused"],
        )

    def test_dotii_config_rejects_duplicate_business_states(self) -> None:
        duplicate = default_dotii_config()
        duplicate["animations"]["complete"]["states"].append("bambu_printing")
        with self.assertRaises(ValueError):
            validate_dotii_config(duplicate)

    def test_dotii_config_unassigned_business_states_keep_plain_status_reason(self) -> None:
        config = default_dotii_config()
        config["animations"]["working"]["states"].remove("bambu_printing")

        validated = validate_dotii_config(config)
        bambu = dotii_state(
            {"configured": True, "connected": True, "status": "printing"},
            True,
            validated,
        )

        self.assertEqual(bambu["expression"], "idle_breath")
        self.assertFalse(bambu["state_assigned"])
        self.assertEqual(bambu["state_duration_ms"], 0)
        self.assertFalse(bambu["state_hold"])
        self.assertEqual(bambu["reason"], "Bambu 正在打印")

    def test_dotii_config_validates_animation_duration(self) -> None:
        config = default_dotii_config()
        config["animations"]["working"]["state_duration_ms"] = 2400
        with self.assertRaises(ValueError):
            validate_dotii_config(config)

        for duration in (0, 1000, 3000, 5000, 30000):
            config["animations"]["working"]["state_duration_ms"] = duration
            self.assertEqual(
                validate_dotii_config(config)["animations"]["working"]["state_duration_ms"],
                duration,
            )

    def test_dotii_config_migrates_removed_happy_animation_to_complete(self) -> None:
        config = default_dotii_config()
        config["animations"]["complete"]["states"].remove("bambu_completed")
        config["animations"]["happy"] = {
            "states": ["bambu_completed"],
            "state_duration_ms": 3000,
        }

        migrated = validate_dotii_config(config)

        self.assertNotIn("happy", migrated["animations"])
        self.assertIn("bambu_completed", migrated["animations"]["complete"]["states"])

    def test_dotii_config_migrates_old_editable_fixed_states_to_read_only_defaults(self) -> None:
        config = default_dotii_config()
        for animation in config["animations"].values():
            animation["duration_ms"] = 2400
            animation.pop("state_duration_ms", None)
        config["animations"]["idle_breath"]["states"].append("idle")
        config["animations"]["blink"]["states"].append("blink")
        config["animations"]["touch_response"]["states"].extend(["touch", "bambu_printing"])
        config["animations"]["working"]["states"].remove("bambu_printing")

        migrated = validate_dotii_config(config)

        self.assertEqual(migrated["animations"]["idle_breath"]["states"], [])
        self.assertEqual(migrated["animations"]["touch_response"]["states"], [])
        self.assertIn("bambu_printing", migrated["animations"]["working"]["states"])
        self.assertEqual(migrated["animations"]["working"]["state_duration_ms"], 0)
        self.assertEqual(len(migrated["fixed_states"]), 5)
        self.assertEqual([group["id"] for group in migrated["state_groups"]], ["bambu"])

    def test_dotii_state_keeps_fixed_local_mapping_and_loop_durations(self) -> None:
        state = dotii_state({}, True)
        self.assertEqual(state["local_states"]["touch"]["expression"], "touch_response")
        self.assertEqual(state["local_states"]["touch"]["duration_ms"], 1200)
        self.assertEqual(state["local_states"]["blink"]["expression"], "blink")
        self.assertEqual(state["local_states"]["long_idle"]["expression"], "sleepy_yawn")
        self.assertEqual(len(state["durations_ms"]), 9)

    def test_dotii_state_token_changes_only_with_logical_state(self) -> None:
        printing = dotii_state({"configured": True, "connected": True, "status": "printing"}, True)
        printing_again = dotii_state({"configured": True, "connected": True, "status": "printing"}, True)
        fault = dotii_state({"configured": True, "connected": True, "status": "fault"}, True)
        self.assertEqual(printing["state_token"], printing_again["state_token"])
        self.assertNotEqual(printing["state_token"], fault["state_token"])

    def test_display_config_ignores_removed_codex_ui_field(self) -> None:
        self.assertNotIn("codex_ui", validate_display_config(None))
        self.assertNotIn("codex_ui", validate_display_config({"codex_ui": "dual_limit"}))

    def test_display_config_accepts_screen_off_page_targets(self) -> None:
        for target in ("none", "custom", "dotii"):
            self.assertEqual(
                validate_display_config({"screen_off_page": target})["screen_off_page"],
                target,
            )

    def test_display_config_rejects_unknown_screen_off_page_target(self) -> None:
        with self.assertRaises(ValueError):
            validate_display_config({"screen_off_page": "bambu"})

    def test_startup_command_uses_absolute_paths_and_startup_mode(self) -> None:
        with temporary_directory() as temporary:
            folder = Path(temporary)
            pythonw = folder / "pythonw.exe"
            app_script = folder / "Dotii 管理中心" / "bridge_app.py"
            pythonw.touch()
            app_script.parent.mkdir()
            app_script.touch()

            with mock.patch("codex_bridge.current_platform", return_value=WindowsPlatformAdapter()):
                self.assertEqual(
                    startup_command(pythonw, app_script),
                    f'"{pythonw.resolve()}" -B "{app_script.resolve()}" --startup',
                )

    def test_packaged_startup_targets_management_center_not_bridge(self) -> None:
        with temporary_directory() as temporary:
            management_center = Path(temporary) / "DotiiManagementCenter.exe"
            management_center.touch()
            with mock.patch("codex_bridge.current_platform", return_value=WindowsPlatformAdapter()):
                command = packaged_startup_command(management_center)
            self.assertEqual(command, f'"{management_center.resolve()}" --startup')
            self.assertNotIn("DotiiBridge.exe", command)

    def test_display_config_migrates_charging_timeouts_from_existing_pair(self) -> None:
        display = validate_display_config({
            "screen_off_timeout_seconds": 30,
            "sleep_timeout_seconds": 180,
        })
        self.assertEqual(display["charging_screen_off_timeout_seconds"], 30)
        self.assertEqual(display["charging_sleep_timeout_seconds"], 180)

    def test_display_config_keeps_independent_power_timeout_pairs(self) -> None:
        display = validate_display_config({
            "screen_off_timeout_seconds": 30,
            "sleep_timeout_seconds": 180,
            "charging_screen_off_timeout_seconds": 300,
            "charging_sleep_timeout_seconds": 0,
        })
        self.assertEqual(display["screen_off_timeout_seconds"], 30)
        self.assertEqual(display["sleep_timeout_seconds"], 180)
        self.assertEqual(display["charging_screen_off_timeout_seconds"], 300)
        self.assertEqual(display["charging_sleep_timeout_seconds"], 0)

    def test_display_config_rejects_invalid_charging_timeout_order(self) -> None:
        with self.assertRaises(ValueError):
            validate_display_config({
                "charging_screen_off_timeout_seconds": 300,
                "charging_sleep_timeout_seconds": 60,
            })


if __name__ == "__main__":
    unittest.main()
