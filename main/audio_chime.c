#include "audio_chime.h"

#include <stdint.h>
#include <string.h>
#include <time.h>

#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs.h"
#include "voice_assets.h"

#define TAG "audio_chime"

#define CHIME_SAMPLE_RATE 16000
#define CHIME_VOLUME_PERCENT 100
#define CHIME_BLOCK_SAMPLES 256
#define CHIME_QUEUE_LENGTH 4
#define CHIME_TASK_STACK 4096
#define CHIME_TASK_PRIORITY 4
#define CHIME_STATE_NAMESPACE "audio_chime"
#define CHIME_HOUR_KEY "hour"
#define CHIME_JOB_KEY "job"

typedef enum {
    CHIME_HOURLY = 0,
    CHIME_PRINT_DONE,
} chime_kind_t;

static esp_codec_dev_handle_t s_speaker;
static QueueHandle_t s_queue;
static bool s_ready;

/* Open the codec with the boosted curve shared by tones and voice. */
static bool chime_open(void)
{
    if (s_speaker == NULL) return false;
    esp_codec_dev_sample_info_t info = {
        .bits_per_sample = 16,
        .channel = 1,
        .sample_rate = CHIME_SAMPLE_RATE,
    };
    if (esp_codec_dev_open(s_speaker, &info) != ESP_OK) {
        ESP_LOGW(TAG, "codec open failed");
        return false;
    }
    /* The default curve maps volume 100 to 0 dB, which leaves the ES8311 DAC
       at only ~3.6 dB (hw_gain compensation). Boost the top so the 10 mm
       speaker is audible: 100 -> +15 dB. Adding hw_gain compensation (~3.6 dB)
       puts the DAC register near 228/255, still under the +32 dB ceiling. */
    static const esp_codec_dev_vol_map_t vol_map[] = {
        {0, -50.0f},
        {100, 15.0f},
    };
    esp_codec_dev_vol_curve_t curve = {(esp_codec_dev_vol_map_t *)vol_map, 2};
    esp_codec_dev_set_vol_curve(s_speaker, &curve);
    esp_codec_dev_set_out_vol(s_speaker, CHIME_VOLUME_PERCENT);
    return true;
}

/* Stream a pre-generated 16 kHz / s16le / mono voice prompt. */
static void voice_play(const int16_t *pcm, size_t samples)
{
    if (pcm == NULL || samples == 0) return;
    if (!chime_open()) return;
    static int16_t block[CHIME_BLOCK_SAMPLES];
    for (size_t offset = 0; offset < samples; offset += CHIME_BLOCK_SAMPLES) {
        size_t count = samples - offset;
        if (count > CHIME_BLOCK_SAMPLES) count = CHIME_BLOCK_SAMPLES;
        memcpy(block, pcm + offset, count * sizeof(int16_t));
        esp_codec_dev_write(s_speaker, block, count * sizeof(int16_t));
    }
    esp_codec_dev_close(s_speaker);
}

static void audio_task(void *argument)
{
    (void)argument;
    chime_kind_t kind;
    while (xQueueReceive(s_queue, &kind, portMAX_DELAY) == pdTRUE) {
        if (kind == CHIME_HOURLY) {
            time_t now = time(NULL);
            struct tm local;
            if (localtime_r(&now, &local) == NULL) continue;
            const int hour = local.tm_hour;
            if (hour < 0 || hour > 23) continue;
            switch (hour) {
                case 0:  voice_play(voice_hour_00, VOICE_HOUR_00_LEN); break;
                case 1:  voice_play(voice_hour_01, VOICE_HOUR_01_LEN); break;
                case 2:  voice_play(voice_hour_02, VOICE_HOUR_02_LEN); break;
                case 3:  voice_play(voice_hour_03, VOICE_HOUR_03_LEN); break;
                case 4:  voice_play(voice_hour_04, VOICE_HOUR_04_LEN); break;
                case 5:  voice_play(voice_hour_05, VOICE_HOUR_05_LEN); break;
                case 6:  voice_play(voice_hour_06, VOICE_HOUR_06_LEN); break;
                case 7:  voice_play(voice_hour_07, VOICE_HOUR_07_LEN); break;
                case 8:  voice_play(voice_hour_08, VOICE_HOUR_08_LEN); break;
                case 9:  voice_play(voice_hour_09, VOICE_HOUR_09_LEN); break;
                case 10: voice_play(voice_hour_10, VOICE_HOUR_10_LEN); break;
                case 11: voice_play(voice_hour_11, VOICE_HOUR_11_LEN); break;
                case 12: voice_play(voice_hour_12, VOICE_HOUR_12_LEN); break;
                case 13: voice_play(voice_hour_13, VOICE_HOUR_13_LEN); break;
                case 14: voice_play(voice_hour_14, VOICE_HOUR_14_LEN); break;
                case 15: voice_play(voice_hour_15, VOICE_HOUR_15_LEN); break;
                case 16: voice_play(voice_hour_16, VOICE_HOUR_16_LEN); break;
                case 17: voice_play(voice_hour_17, VOICE_HOUR_17_LEN); break;
                case 18: voice_play(voice_hour_18, VOICE_HOUR_18_LEN); break;
                case 19: voice_play(voice_hour_19, VOICE_HOUR_19_LEN); break;
                case 20: voice_play(voice_hour_20, VOICE_HOUR_20_LEN); break;
                case 21: voice_play(voice_hour_21, VOICE_HOUR_21_LEN); break;
                case 22: voice_play(voice_hour_22, VOICE_HOUR_22_LEN); break;
                case 23: voice_play(voice_hour_23, VOICE_HOUR_23_LEN); break;
                default: break;
            }
        } else {
            voice_play(voice_print_done, VOICE_PRINT_DONE_LEN);
        }
    }
}

esp_err_t audio_chime_init(void)
{
    if (s_ready) return ESP_OK;
    s_speaker = bsp_audio_codec_speaker_init();
    if (s_speaker == NULL) {
        ESP_LOGW(TAG, "speaker codec unavailable; notifications stay silent");
        return ESP_ERR_NOT_SUPPORTED;
    }
    s_queue = xQueueCreate(CHIME_QUEUE_LENGTH, sizeof(chime_kind_t));
    if (s_queue == NULL) {
        ESP_LOGE(TAG, "unable to create chime queue");
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(audio_task, "audio_chime", CHIME_TASK_STACK, NULL,
                    CHIME_TASK_PRIORITY, NULL) != pdPASS) {
        ESP_LOGE(TAG, "unable to create chime task");
        vQueueDelete(s_queue);
        s_queue = NULL;
        return ESP_ERR_NO_MEM;
    }
    s_ready = true;
    ESP_LOGI(TAG, "SPK ready on ES8311 (I2S DOUT=%d, PA=%d)",
             (int)BSP_I2S_DOUT, (int)BSP_POWER_AMP_IO);
    return ESP_OK;
}

bool audio_chime_ready(void)
{
    return s_ready;
}

static bool chime_in_quiet_hours(void)
{
    time_t now = time(NULL);
    struct tm local;
    if (localtime_r(&now, &local) == NULL) return false;
    /* Before SNTP sync the clock is meaningless; do not silently mute then. */
    if (local.tm_year < 120) return false;
    return local.tm_hour >= AUDIO_CHIME_QUIET_START_HOUR ||
           local.tm_hour < AUDIO_CHIME_QUIET_END_HOUR;
}

/* Persistent dedupe: a deep-sleep wake is a reboot, so RAM-only markers would
   let an already announced chime play again. */
static uint32_t chime_state_get(const char *key, bool *found)
{
    *found = false;
    nvs_handle_t handle;
    if (nvs_open(CHIME_STATE_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return 0;
    uint32_t value = 0;
    *found = nvs_get_u32(handle, key, &value) == ESP_OK;
    nvs_close(handle);
    return value;
}

static void chime_state_set(const char *key, uint32_t value)
{
    nvs_handle_t handle;
    if (nvs_open(CHIME_STATE_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return;
    if (nvs_set_u32(handle, key, value) == ESP_OK) nvs_commit(handle);
    nvs_close(handle);
}

bool audio_chime_claim_hour(int32_t hour_stamp)
{
    bool found;
    const uint32_t stamp = (uint32_t)hour_stamp;
    if (chime_state_get(CHIME_HOUR_KEY, &found) == stamp && found) return false;
    chime_state_set(CHIME_HOUR_KEY, stamp);
    return true;
}

bool audio_chime_claim_print(uint32_t job_key)
{
    if (job_key == 0) return true;
    bool found;
    if (chime_state_get(CHIME_JOB_KEY, &found) == job_key && found) return false;
    chime_state_set(CHIME_JOB_KEY, job_key);
    return true;
}

static void chime_request(chime_kind_t kind)
{
    if (!s_ready || s_queue == NULL) return;
    if (chime_in_quiet_hours()) return;
    /* A burst of events must never block the caller: drop when the queue is full. */
    (void)xQueueSend(s_queue, &kind, 0);
}

void audio_chime_hourly(void)
{
    chime_request(CHIME_HOURLY);
}

void audio_chime_print_done(void)
{
    chime_request(CHIME_PRINT_DONE);
}
