#include "audio_chime.h"

#include <math.h>
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

#define TAG "audio_chime"

#define CHIME_SAMPLE_RATE 16000
#define CHIME_VOLUME_PERCENT 100
#define CHIME_BLOCK_SAMPLES 256
#define CHIME_FADE_SAMPLES (CHIME_SAMPLE_RATE * 8 / 1000)
#define CHIME_QUEUE_LENGTH 4
#define CHIME_TASK_STACK 4096
#define CHIME_TASK_PRIORITY 4
#define CHIME_STATE_NAMESPACE "audio_chime"
#define CHIME_HOUR_KEY "hour"
#define CHIME_JOB_KEY "job"

#ifndef M_PI_F
#define M_PI_F 3.14159265358979323846f
#endif

typedef enum {
    CHIME_HOURLY = 0,
    CHIME_PRINT_DONE,
} chime_kind_t;

typedef struct {
    float frequency_hz; /* 0 renders silence */
    uint16_t duration_ms;
} chime_note_t;

/* Hourly descending three-tone chime, moved into the 10 mm speaker's
   720-1080 Hz resonance band and lengthened for audibility. Distinct from the
   print-done rhythm by its descending pitch. */
static const chime_note_t HOURLY_NOTES[] = {
    {1046.5f, 450}, /* C6 */
    {880.0f, 450},  /* A5 */
    {784.0f, 900},  /* G5, long finish */
};

/* "ding · ding —— ding~" rhythm at 1 kHz, the 10 mm speaker's resonance
   peak. Pitch variation was inaudible on the small driver, so the print-done
   chime is distinguished from the hourly descent by rhythm alone. */
static const chime_note_t PRINT_DONE_NOTES[] = {
    {1000.0f, 150},
    {0.0f, 90},      /* pause */
    {1000.0f, 150},
    {0.0f, 90},      /* pause */
    {1000.0f, 500},  /* long finish */
};

static esp_codec_dev_handle_t s_speaker;
static QueueHandle_t s_queue;
static bool s_ready;

static void chime_write_block(const chime_note_t *note, int offset, int count)
{
    static int16_t samples[CHIME_BLOCK_SAMPLES];
    for (int index = 0; index < count; ++index) {
        const int position = offset + index;
        if (note->frequency_hz <= 0.0f) {
            samples[index] = 0;
            continue;
        }
        /* Attack/release ramp keeps every note click-free. */
        const int attack = position < CHIME_FADE_SAMPLES ? position : CHIME_FADE_SAMPLES;
        int release = (int)((uint32_t)note->duration_ms * CHIME_SAMPLE_RATE / 1000U) - position;
        if (release > CHIME_FADE_SAMPLES) release = CHIME_FADE_SAMPLES;
        if (release < 0) release = 0;
        const int ramp = attack < release ? attack : release;
        const float envelope = (float)ramp / (float)CHIME_FADE_SAMPLES;
        const float phase = 2.0f * M_PI_F * note->frequency_hz * (float)position /
                            (float)CHIME_SAMPLE_RATE;
        const float amplitude = envelope * ((float)CHIME_VOLUME_PERCENT / 100.0f) * 32767.0f;
        samples[index] = (int16_t)(sinf(phase) * amplitude);
    }
    esp_codec_dev_write(s_speaker, samples, count * (int)sizeof(int16_t));
}

static void chime_play(const chime_note_t *notes, size_t count)
{
    if (s_speaker == NULL) return;
    esp_codec_dev_sample_info_t info = {
        .bits_per_sample = 16,
        .channel = 1,
        .sample_rate = CHIME_SAMPLE_RATE,
    };
    /* Open per notification: the codec and PA only draw current while audible. */
    if (esp_codec_dev_open(s_speaker, &info) != ESP_OK) {
        ESP_LOGW(TAG, "codec open failed");
        return;
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
    static const chime_note_t pause = {0.0f, 70};
    for (size_t note = 0; note < count; ++note) {
        const int total = (int)((uint32_t)notes[note].duration_ms * CHIME_SAMPLE_RATE / 1000U);
        for (int offset = 0; offset < total; offset += CHIME_BLOCK_SAMPLES) {
            int block = total - offset;
            if (block > CHIME_BLOCK_SAMPLES) block = CHIME_BLOCK_SAMPLES;
            chime_write_block(&notes[note], offset, block);
        }
        if (note + 1 < count) {
            const int gap = (int)((uint32_t)pause.duration_ms * CHIME_SAMPLE_RATE / 1000U);
            for (int offset = 0; offset < gap; offset += CHIME_BLOCK_SAMPLES) {
                int block = gap - offset;
                if (block > CHIME_BLOCK_SAMPLES) block = CHIME_BLOCK_SAMPLES;
                chime_write_block(&pause, offset, block);
            }
        }
    }
    esp_codec_dev_close(s_speaker);
}

static void audio_task(void *argument)
{
    (void)argument;
    chime_kind_t kind;
    while (xQueueReceive(s_queue, &kind, portMAX_DELAY) == pdTRUE) {
        if (kind == CHIME_HOURLY) {
            chime_play(HOURLY_NOTES, sizeof(HOURLY_NOTES) / sizeof(HOURLY_NOTES[0]));
        } else {
            chime_play(PRINT_DONE_NOTES, sizeof(PRINT_DONE_NOTES) / sizeof(PRINT_DONE_NOTES[0]));
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
