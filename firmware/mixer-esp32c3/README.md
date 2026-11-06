# Mixer-drum temperature sensor — ESP32-C3

Firmware for the box that rides on a mixer and reports the concrete temperature
per load to the dispatch app (`/api/mixer/load`). One board per truck.

## Parts
- ESP32-C3 board (ESP32-C3-DevKitM-1 or Seeed XIAO ESP32C3), USB-C cable
- DS18B20 **waterproof** probe (stainless tip), 4.7 kΩ resistor
- Power: the truck's 12 V through a 5 V buck converter into USB, or a single-cell
  LiPo through the board's battery input (then wire the divider for the battery readout)

## Wiring
| Probe wire | Goes to |
|---|---|
| Red (VCC) | 3V3 |
| Black (GND) | GND |
| Yellow (DATA) | GPIO 4 (`ONEWIRE_PIN`), with the 4.7 kΩ resistor between DATA and 3V3 |

Battery readout (optional): two equal resistors from BAT+ to GND, the midpoint to
GPIO 2 (`BATT_ADC_PIN`). Set `BATT_ADC_PIN -1` if the box runs off the truck.

## Flashing (Arduino IDE)
1. Arduino IDE 2.x → File → Preferences → Additional boards manager URLs, add
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Tools → Board → Boards Manager → install **esp32 by Espressif Systems** (3.x).
3. Tools → Manage Libraries → install **OneWire** and **DallasTemperature**.
4. Open `mixer-esp32c3.ino`. Edit `config.h`: `TRUCK_LABEL` (exactly as on the
   board, e.g. `RTS 4111`), Wi-Fi, and `DEVICE_KEY` (the `MIXER_DEVICE_KEY` set on
   Render — the office has it).
5. Tools → Board → **ESP32C3 Dev Module**; **USB CDC On Boot: Enabled**;
   Tools → Port → the board's COM/tty port. If no port shows, hold **BOOT**, tap
   **RESET**, release BOOT (download mode), then try again.
6. Sketch → Upload. Open Serial Monitor at 115200 to watch it join Wi-Fi, find the
   probe (`1 DS18B20 found`) and start polling.

### Bench check before it goes on the truck
Set `BENCH_TEST 1`, upload, watch the Serial Monitor: it posts one fake load,
prints `[post] 200`, then deletes it. Then set `BENCH_TEST 0` and upload again.
(If you see `401`, the device key is wrong. `-1` means it can't reach the API.)

## How it behaves on the truck
- Polls dispatch every 30 s. While the truck's order or load is **Loading at
  yard / En route / On site**, it samples the probe every 15 s.
- When dispatch moves the truck past On site (returning / complete), it posts one
  summary: start, end, average temperature (°C and °F), battery. The reading
  shows on the truck's fleet card and the Mixer screen.
- If the post fails (no signal at the site) it's kept in flash and retried every
  loop, including after a power cycle. The LED is on while a load is active.
- Reboots, Wi-Fi drops and clock resyncs are handled on their own.

## Settings that matter
`config.h` — truck label, Wi-Fi, API base + device key, pins, timings.
Change only `TRUCK_LABEL` (and Wi-Fi if the hotspot differs) when flashing a
second board for another truck.
