#include "ui_language.h"
#include "app_state.h"

#include <string.h>

#include "esp_timer.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"

static QueueHandle_t s_snapshot_queue;

QueueHandle_t app_state_queue_create(void)
{
    if (s_snapshot_queue == NULL) {
        s_snapshot_queue = xQueueCreate(1, sizeof(display_snapshot_t));
    }
    return s_snapshot_queue;
}

void app_state_publish(const display_snapshot_t *snapshot)
{
    if (s_snapshot_queue != NULL && snapshot != NULL) {
        xQueueOverwrite(s_snapshot_queue, snapshot);
    }
}

void app_state_make_preview(display_snapshot_t *snapshot)
{
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->valid = true;
    snapshot->preview_data = true;
    snapshot->source_online = false;
    snapshot->weather_enabled = true;
    snapshot->bambu_enabled = true;
    snapshot->custom_enabled = true;
    snapshot->dotii_enabled = true;
    snapshot->custom_ring_enabled = true;
    snapshot->bambu_configured = true;
    snapshot->bambu_connected = true;
    snapshot->bambu_commandable = true;
    snapshot->docked_rotation_tenths = 900;
    snapshot->screen_off_timeout_seconds = 60;
    snapshot->sleep_timeout_seconds = 300;
    snapshot->charging_screen_off_timeout_seconds = 60;
    snapshot->charging_sleep_timeout_seconds = 300;
    snapshot->bambu_status = BAMBU_STATUS_PRINTING;
    snapshot->dotii_expression = DOTII_EXPRESSION_WORKING;
    snapshot->dotii_state_assigned = true;
    snapshot->dotii_state_duration_ms = 0;
    snapshot->dotii_state_token = 1;
    snapshot->dotii_touch_expression = DOTII_EXPRESSION_TOUCH_RESPONSE;
    snapshot->dotii_blink_expression = DOTII_EXPRESSION_BLINK;
    snapshot->dotii_long_idle_expression = DOTII_EXPRESSION_SLEEPY_YAWN;
    for (size_t index = 0; index < DOTII_EXPRESSION_COUNT; index++) {
        snapshot->dotii_expression_durations_ms[index] = 800;
    }
    snapshot->dotii_expression_durations_ms[DOTII_EXPRESSION_TOUCH_RESPONSE] = 1200;
    snapshot->bambu_progress = 68;
    snapshot->bambu_remaining_minutes = 42;
    snapshot->bambu_layer_current = 87;
    snapshot->bambu_layer_total = 142;
    snapshot->bambu_nozzle_temperature = 220;
    snapshot->bambu_bed_temperature = 55;
    snapshot->bambu_finish_at = time(NULL) + 42 * 60;
    snapshot->bambu_updated_at = time(NULL);
    snapshot->weather.enabled = true;
    snapshot->weather.available = true;
    snapshot->weather.condition_code = 1;
    snapshot->weather.temperature_c = 26;
    snapshot->weather.temp_low_c = 19;
    snapshot->weather.temp_high_c = 29;
    snapshot->weather.humidity_percent = 62;
    snapshot->weather.wind_kph = 9;
    strlcpy(snapshot->weather.city, "Shanghai", sizeof(snapshot->weather.city));
    strlcpy(snapshot->lunar_text, ui_tr("农历丙午年十月初六 壬辰日"), sizeof(snapshot->lunar_text));
    snapshot->custom_accent = 0xF2C66D;
    snapshot->custom_ring_start = 0xF2C66D;
    snapshot->custom_ring_end = 0x5DA9FF;
    snapshot->generated_at = time(NULL);
    strlcpy(snapshot->custom_title, ui_tr("我的页面"), sizeof(snapshot->custom_title));
    strlcpy(snapshot->custom_value, ui_tr("你好，Dotii"), sizeof(snapshot->custom_value));
    strlcpy(snapshot->custom_body, ui_tr("这是从 Dotii 管理中心同步的自定义界面。"), sizeof(snapshot->custom_body));
    strlcpy(snapshot->custom_footer, ui_tr("自定义内容"), sizeof(snapshot->custom_footer));
    strlcpy(snapshot->bambu_name, "Bambu Lab", sizeof(snapshot->bambu_name));
    strlcpy(snapshot->bambu_status_text, ui_tr("打印中"), sizeof(snapshot->bambu_status_text));
    strlcpy(snapshot->bambu_filename, ui_tr("可爱机器人外壳.3mf"), sizeof(snapshot->bambu_filename));
    strlcpy(snapshot->bambu_filament, "PLA Basic", sizeof(snapshot->bambu_filament));
}

/* Deep sleep is a full reboot, so everything above is lost. Cache the last real
   snapshot in NVS so a wake without the management centre restores every page
   (including Dotii expressions and Bambu metadata) instead of demo data. */
#define SNAPSHOT_CACHE_NAMESPACE "ui_cache"
#define SNAPSHOT_CACHE_KEY "snap"
#define SNAPSHOT_CACHE_MAGIC 0x534E5034u
#define SNAPSHOT_CACHE_MIN_INTERVAL_US (60LL * 1000000LL)

typedef struct {
    uint32_t magic;
    uint32_t payload_size;
    /* Keep the whole snapshot. Hand-picked field lists kept dropping newly
       added pages (Dotii expressions, camera metadata) after a deep-sleep
       reboot, which disabled the Dotii page and froze the Bambu view. */
    display_snapshot_t snapshot;
} snapshot_cache_t;

static snapshot_cache_t s_stored_cache;
static bool s_stored_cache_valid;
static int64_t s_stored_cache_us;

static void cache_fill(const display_snapshot_t *snapshot, snapshot_cache_t *cache)
{
    memset(cache, 0, sizeof(*cache));
    cache->magic = SNAPSHOT_CACHE_MAGIC;
    cache->payload_size = sizeof(display_snapshot_t);
    cache->snapshot = *snapshot;
    /* These change on every poll or are derived when restoring; clearing them
       keeps the memcmp dedupe from rewriting the flash needlessly. */
    cache->snapshot.generated_at = 0;
    cache->snapshot.valid = false;
    cache->snapshot.preview_data = false;
    cache->snapshot.source_online = false;
    cache->snapshot.stale = false;
    /* Camera frames live only in PSRAM and cannot survive deep sleep. */
    cache->snapshot.bambu_camera_available = false;
    cache->snapshot.bambu_camera_revision = 0;
    cache->snapshot.bambu_camera_size = 0;
}

void app_state_cache_store(const display_snapshot_t *snapshot)
{
    /* Only cache payloads that came from the management centre: the local demo
       snapshot is marked preview_data too, but it never sets source_online. */
    if (snapshot == NULL || !snapshot->valid || !snapshot->source_online) return;
    snapshot_cache_t cache;
    cache_fill(snapshot, &cache);
    if (s_stored_cache_valid && memcmp(&cache, &s_stored_cache, sizeof(cache)) == 0) return;
    const int64_t now = esp_timer_get_time();
    /* Printing progress changes every poll; cap the flash writes. */
    if (s_stored_cache_valid && now - s_stored_cache_us < SNAPSHOT_CACHE_MIN_INTERVAL_US) return;
    nvs_handle_t handle;
    if (nvs_open(SNAPSHOT_CACHE_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return;
    esp_err_t result = nvs_set_blob(handle, SNAPSHOT_CACHE_KEY, &cache, sizeof(cache));
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    if (result != ESP_OK) return;
    s_stored_cache = cache;
    s_stored_cache_valid = true;
    s_stored_cache_us = now;
}

bool app_state_cache_restore(display_snapshot_t *snapshot)
{
    if (snapshot == NULL) return false;
    nvs_handle_t handle;
    if (nvs_open(SNAPSHOT_CACHE_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return false;
    snapshot_cache_t cache;
    size_t size = sizeof(cache);
    esp_err_t result = nvs_get_blob(handle, SNAPSHOT_CACHE_KEY, &cache, &size);
    nvs_close(handle);
    if (result != ESP_OK || size != sizeof(cache) || cache.magic != SNAPSHOT_CACHE_MAGIC ||
        cache.payload_size != sizeof(display_snapshot_t)) return false;
    *snapshot = cache.snapshot;
    /* Restored data is real but not live: the UI reports it as offline. */
    snapshot->valid = true;
    snapshot->preview_data = false;
    snapshot->source_online = false;
    snapshot->stale = true;
    /* No camera frame survives a reboot, even though the rest is restored. */
    snapshot->bambu_camera_available = false;
    return true;
}

const char *app_state_bambu_status_text(bambu_status_t status)
{
    switch (status) {
    case BAMBU_STATUS_IDLE: return ui_tr("空闲");
    case BAMBU_STATUS_PREPARING: return ui_tr("准备中");
    case BAMBU_STATUS_PRINTING: return ui_tr("打印中");
    case BAMBU_STATUS_PAUSED: return ui_tr("已暂停");
    case BAMBU_STATUS_COMPLETED: return ui_tr("已完成");
    case BAMBU_STATUS_CANCELLING: return ui_tr("取消中");
    case BAMBU_STATUS_FAULT: return ui_tr("故障");
    default: return ui_tr("离线");
    }
}

bambu_status_t app_state_bambu_status_from_string(const char *status)
{
    if (status == NULL) return BAMBU_STATUS_OFFLINE;
    if (strcmp(status, "idle") == 0) return BAMBU_STATUS_IDLE;
    if (strcmp(status, "preparing") == 0) return BAMBU_STATUS_PREPARING;
    if (strcmp(status, "printing") == 0) return BAMBU_STATUS_PRINTING;
    if (strcmp(status, "paused") == 0) return BAMBU_STATUS_PAUSED;
    if (strcmp(status, "completed") == 0) return BAMBU_STATUS_COMPLETED;
    if (strcmp(status, "cancelling") == 0) return BAMBU_STATUS_CANCELLING;
    if (strcmp(status, "fault") == 0) return BAMBU_STATUS_FAULT;
    return BAMBU_STATUS_OFFLINE;
}

dotii_expression_t app_state_dotii_expression_from_string(const char *expression)
{
    if (expression == NULL) return DOTII_EXPRESSION_IDLE_BREATH;
    if (strcmp(expression, "blink") == 0) return DOTII_EXPRESSION_BLINK;
    if (strcmp(expression, "curious") == 0) return DOTII_EXPRESSION_CURIOUS;
    if (strcmp(expression, "happy") == 0) return DOTII_EXPRESSION_COMPLETE;
    if (strcmp(expression, "sleepy_yawn") == 0) return DOTII_EXPRESSION_SLEEPY_YAWN;
    if (strcmp(expression, "touch_response") == 0) return DOTII_EXPRESSION_TOUCH_RESPONSE;
    if (strcmp(expression, "connecting") == 0) return DOTII_EXPRESSION_CONNECTING;
    if (strcmp(expression, "working") == 0) return DOTII_EXPRESSION_WORKING;
    if (strcmp(expression, "complete") == 0) return DOTII_EXPRESSION_COMPLETE;
    if (strcmp(expression, "failure") == 0) return DOTII_EXPRESSION_FAILURE;
    return DOTII_EXPRESSION_IDLE_BREATH;
}

const char *app_state_weather_condition_text(int condition_code)
{
    if (condition_code == 0) return ui_tr("晴");
    if (condition_code == 1) return ui_tr("基本晴");
    if (condition_code == 2) return ui_tr("局部多云");
    if (condition_code == 3) return ui_tr("阴");
    if (condition_code == 45 || condition_code == 48) return ui_tr("雾");
    if (condition_code >= 51 && condition_code <= 57) return ui_tr("毛毛雨");
    if (condition_code == 61 || condition_code == 80) return ui_tr("小雨");
    if (condition_code == 63 || condition_code == 81) return ui_tr("中雨");
    if (condition_code >= 65 && condition_code <= 67) return ui_tr("大雨");
    if (condition_code == 66) return ui_tr("冻雨");
    if (condition_code == 82) return ui_tr("强阵雨");
    if (condition_code == 71 || condition_code == 85 || condition_code == 77) return ui_tr("小雪");
    if (condition_code == 73 || condition_code == 86) return ui_tr("中雪");
    if (condition_code >= 74 && condition_code <= 76) return ui_tr("大雪");
    if (condition_code >= 95 && condition_code <= 99) return ui_tr("雷阵雨");
    return ui_tr("多云");
}
