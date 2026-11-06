// ── Aussieblock mixer temperature sensor — per-truck settings ───────────────
// Edit this file for each board you flash, then upload mixer-esp32c3.ino.
#pragma once

// Which truck this box rides on. Must match the truck's label on the dispatch
// board EXACTLY (Manage trucks), e.g. "RTS 7329".
#define TRUCK_LABEL        "RTS 4111"

// Wi-Fi the box joins. On the road this is normally the driver tablet's hotspot
// or an in-cab hotspot; the box reconnects on its own whenever it drops.
#define WIFI_SSID          "your-hotspot-name"
#define WIFI_PASSWORD      "your-hotspot-password"

// The dispatch API and the shared device secret (MIXER_DEVICE_KEY on Render —
// ask the office; the dev default is "ab-mixer-change-me").
#define API_BASE           "https://aussieblock-api.onrender.com"
#define DEVICE_KEY         "ab-mixer-change-me"

// Wiring (GPIO numbers). DS18B20 data pin needs a 4.7 kΩ pull-up to 3.3 V.
#define ONEWIRE_PIN        4      // DS18B20 waterproof probe on the drum
#define BATT_ADC_PIN       2      // battery sense via divider; set -1 if powered from the truck
#define BATT_DIVIDER       2.0f   // (R1+R2)/R2 — 2.0 for two equal resistors
#define BATT_EMPTY_V       3.30f  // 0 %
#define BATT_FULL_V        4.20f  // 100 % (single-cell LiPo)
#define STATUS_LED_PIN     8      // on-board LED on the ESP32-C3-DevKitM-1 (XIAO C3 has none: set -1)

// Timing
#define POLL_SECONDS       30     // how often to ask dispatch "is this truck loaded?"
#define SAMPLE_SECONDS     15     // how often to read the drum temperature while loaded
#define WIFI_TIMEOUT_SEC   20

// Set to 1 to post one fake load on boot (then delete it) to prove the key,
// Wi-Fi and API work from the bench. Leave 0 on the truck.
#define BENCH_TEST         0
