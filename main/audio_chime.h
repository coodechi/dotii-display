#pragma once

#include <stdbool.h>

#include "esp_err.h"

/* Quiet hours (local time, inclusive start / exclusive end). Shared with the
   deep-sleep scheduler so the device never wakes just to stay silent. */
#define AUDIO_CHIME_QUIET_START_HOUR 23
#define AUDIO_CHIME_QUIET_END_HOUR 7

/* Notification tones for the onboard ES8311 codec + SPK output.
   Playback runs on its own task so the LVGL timer never blocks. */
esp_err_t audio_chime_init(void);

/* Hourly time signal (整点报时). */
void audio_chime_hourly(void);

/* Print finished notification (拓竹打印完成). */
void audio_chime_print_done(void);

/* Deduplication across deep-sleep reboots. Both return true only when the
   event has not been announced before, and record it in NVS immediately.
   A deep sleep is a full reset: without these an already-played chime can
   replay when the device publishes its cached/demo snapshot first. */
bool audio_chime_claim_hour(int32_t hour_stamp);
bool audio_chime_claim_print(uint32_t job_key);

bool audio_chime_ready(void);
