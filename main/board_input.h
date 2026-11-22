#pragma once

#include <stdint.h>

#include "esp_err.h"

void board_input_prepare_after_wake(void);
esp_err_t board_input_start(void);
void board_input_request_sleep(void);
void board_input_request_shutdown(void);

/* Hour stamp of an RTC hourly-chime wake; -1 when the timer did not wake us.
   Consumed once by app_main so the same hour is never announced twice. */
int32_t board_input_take_pending_chime(void);
