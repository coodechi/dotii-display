#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

typedef enum {
    BAMBU_STATUS_OFFLINE = 0,
    BAMBU_STATUS_IDLE,
    BAMBU_STATUS_PREPARING,
    BAMBU_STATUS_PRINTING,
    BAMBU_STATUS_PAUSED,
    BAMBU_STATUS_COMPLETED,
    BAMBU_STATUS_CANCELLING,
    BAMBU_STATUS_FAULT,
} bambu_status_t;

typedef enum {
    DOTII_EXPRESSION_IDLE_BREATH = 0,
    DOTII_EXPRESSION_BLINK,
    DOTII_EXPRESSION_CURIOUS,
    DOTII_EXPRESSION_SLEEPY_YAWN,
    DOTII_EXPRESSION_TOUCH_RESPONSE,
    DOTII_EXPRESSION_CONNECTING,
    DOTII_EXPRESSION_WORKING,
    DOTII_EXPRESSION_COMPLETE,
    DOTII_EXPRESSION_FAILURE,
    DOTII_EXPRESSION_COUNT,
} dotii_expression_t;

typedef enum {
    DISPLAY_CLOCK_STYLE_CLASSIC = 0,
    DISPLAY_CLOCK_STYLE_MINIMAL,
    DISPLAY_CLOCK_STYLE_SUNSET,
    DISPLAY_CLOCK_STYLE_MINT,
} display_clock_style_t;

typedef enum {
    DISPLAY_SCREEN_OFF_PAGE_NONE = 0,
    DISPLAY_SCREEN_OFF_PAGE_CUSTOM,
    DISPLAY_SCREEN_OFF_PAGE_DOTII,
} display_screen_off_page_t;

typedef struct {
    bool enabled;
    bool available;
    int condition_code;
    int temperature_c;
    int temp_low_c;
    int temp_high_c;
    int humidity_percent;
    int wind_kph;
    char city[33];
} weather_status_t;

typedef struct {
    bool valid;
    bool preview_data;
    bool source_online;
    bool stale;
    bool weather_enabled;
    bool bambu_enabled;
    bool custom_enabled;
    bool dotii_enabled;
    bool custom_image_available;
    bool custom_ring_enabled;
    bool bambu_configured;
    bool bambu_connected;
    bool bambu_commandable;
    bool bambu_camera_available;
    bool dotii_base_idle;
    bool dotii_state_assigned;
    uint32_t display_revision;
    int16_t docked_rotation_tenths;
    uint32_t screen_off_timeout_seconds;
    uint32_t sleep_timeout_seconds;
    uint32_t charging_screen_off_timeout_seconds;
    uint32_t charging_sleep_timeout_seconds;
    display_screen_off_page_t screen_off_page;
    display_clock_style_t clock_style;
    uint32_t custom_accent;
    uint32_t custom_ring_start;
    uint32_t custom_ring_end;
    uint32_t custom_image_revision;
    uint32_t custom_image_size;
    bambu_status_t bambu_status;
    dotii_expression_t dotii_expression;
    uint32_t dotii_state_duration_ms;
    uint32_t dotii_state_token;
    dotii_expression_t dotii_touch_expression;
    dotii_expression_t dotii_blink_expression;
    dotii_expression_t dotii_long_idle_expression;
    uint16_t dotii_expression_durations_ms[DOTII_EXPRESSION_COUNT];
    uint8_t bambu_progress;
    uint16_t bambu_remaining_minutes;
    uint16_t bambu_layer_current;
    uint16_t bambu_layer_total;
    int16_t bambu_nozzle_temperature;
    int16_t bambu_bed_temperature;
    uint32_t bambu_camera_revision;
    uint32_t bambu_camera_size;
    time_t bambu_finish_at;
    time_t bambu_updated_at;
    char bambu_name[41];
    char bambu_status_text[17];
    char bambu_filename[96];
    char bambu_filament[32];
    char custom_title[49];
    char custom_value[97];
    char custom_body[257];
    char custom_footer[97];
    weather_status_t weather;
    char lunar_text[65];
    time_t generated_at;
} display_snapshot_t;

QueueHandle_t app_state_queue_create(void);
void app_state_publish(const display_snapshot_t *snapshot);
void app_state_make_preview(display_snapshot_t *snapshot);
/* Deep sleep wipes RAM, so the last real snapshot is cached in NVS and
   restored at boot instead of falling back to demo data. */
void app_state_cache_store(const display_snapshot_t *snapshot);
bool app_state_cache_restore(display_snapshot_t *snapshot);
const char *app_state_bambu_status_text(bambu_status_t status);
bambu_status_t app_state_bambu_status_from_string(const char *status);
dotii_expression_t app_state_dotii_expression_from_string(const char *expression);
const char *app_state_weather_condition_text(int condition_code);
