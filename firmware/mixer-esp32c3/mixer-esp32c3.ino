/*
  Aussieblock mixer-drum temperature sensor — ESP32-C3 firmware   (fw 1.0.0)

  What it does
  ────────────
  • Joins Wi-Fi (the cab hotspot), keeps the clock in sync with NTP.
  • Every POLL_SECONDS asks the dispatch API whether THIS truck is carrying a
    confirmed load:   GET /api/mixer/active-load?truck=<label>
    (dispatch shows the truck batched / en route / on site).
  • While a load is active it reads the DS18B20 probe on the drum every
    SAMPLE_SECONDS and keeps the running average, min and max.
  • When the load ends (dispatch no longer shows it active) it posts ONE
    summary:          POST /api/mixer/load   {load_uid, truck_id, started_at,
                      ended_at, mix_temp_c, mix_temp_f, batt_v, batt_pct, fw}
    The office sees the concrete temperature on the truck's card and in the
    Mixer screen. Posts are retried and survive a reboot (kept in flash).
  • Both calls carry the shared secret in the X-Device-Key header.

  Libraries (Arduino IDE → Library Manager): OneWire, DallasTemperature.
  Board:  "ESP32C3 Dev Module" (esp32 by Espressif ≥ 3.0), USB CDC On Boot: Enabled.
  Settings live in config.h — edit it per truck before uploading.
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <time.h>
#include "config.h"

static const char* FW_VERSION = "1.0.0";

OneWire oneWire(ONEWIRE_PIN);
DallasTemperature probe(&oneWire);
Preferences prefs;                 // pending post survives a reboot

// ── load being tracked ──────────────────────────────────────────────────────
struct LoadStats {
  bool   active = false;
  String ref;                      // dispatch order ref, e.g. "AB-24840"
  int    loadSeq = 0;              // pour load number (0 = single delivery)
  time_t startedAt = 0;
  double sumC = 0; int n = 0; float minC = 999, maxC = -999;
} load;

unsigned long lastPoll = 0, lastSample = 0;

// ── helpers ─────────────────────────────────────────────────────────────────
static void led(bool on) { if (STATUS_LED_PIN >= 0) digitalWrite(STATUS_LED_PIN, on ? LOW : HIGH); }  // DevKitM LED is active-low

static bool wifiUp() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.printf("[wifi] joining %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_SEC * 1000UL) { delay(250); Serial.print("."); }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) { Serial.printf("[wifi] up, ip %s\n", WiFi.localIP().toString().c_str()); return true; }
  Serial.println("[wifi] not connected — will retry");
  return false;
}

static bool clockOk() { return time(nullptr) > 1700000000; }   // past Nov 2023 = synced

static void syncClock() {
  if (clockOk()) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 40 && !clockOk(); i++) delay(250);
  Serial.printf("[time] %s\n", clockOk() ? "synced" : "NOT synced yet");
}

static float readBatteryV() {
  if (BATT_ADC_PIN < 0) return -1;
  uint32_t mv = analogReadMilliVolts(BATT_ADC_PIN);
  return (mv / 1000.0f) * BATT_DIVIDER;
}

static float batteryPct(float v) {
  if (v < 0) return -1;
  float p = (v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100.0f;
  return p < 0 ? 0 : (p > 100 ? 100 : p);
}

static float readTempC() {
  probe.requestTemperatures();
  float c = probe.getTempCByIndex(0);
  if (c == DEVICE_DISCONNECTED_C || c < -40 || c > 125) return NAN;
  return c;
}

// Minimal URL-encoding for the truck label ("RTS 7329" -> "RTS%207329").
static String urlEncode(const String& s) {
  String out; char buf[4];
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.') out += c;
    else { snprintf(buf, sizeof buf, "%%%02X", (unsigned char)c); out += buf; }
  }
  return out;
}

// One HTTPS call with the device key. Returns the HTTP status; body in `resp`.
static int apiCall(const char* method, const String& path, const String& body, String& resp) {
  WiFiClientSecure client;
  client.setInsecure();   // telemetry box: trust the server by its name; the device key is the credential
  HTTPClient http;
  http.setTimeout(15000);
  if (!http.begin(client, String(API_BASE) + path)) return -1;
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.addHeader("Content-Type", "application/json");
  int code = (strcmp(method, "POST") == 0) ? http.POST(body)
           : (strcmp(method, "DELETE") == 0) ? http.sendRequest("DELETE")
           : http.GET();
  resp = (code > 0) ? http.getString() : String("");
  http.end();
  return code;
}

// Crude JSON field pulls (the API's replies are small and flat).
static bool jsonBool(const String& j, const char* key) {
  int i = j.indexOf(String("\"") + key + "\""); if (i < 0) return false;
  int c = j.indexOf(':', i); return j.indexOf("true", c) == c + 1 || j.indexOf("true", c) == c + 2;
}
static String jsonStr(const String& j, const char* key) {
  int i = j.indexOf(String("\"") + key + "\""); if (i < 0) return "";
  int q1 = j.indexOf('"', j.indexOf(':', i) + 1); if (q1 < 0) return "";
  int q2 = j.indexOf('"', q1 + 1); return (q2 < 0) ? "" : j.substring(q1 + 1, q2);
}
static int jsonInt(const String& j, const char* key) {
  int i = j.indexOf(String("\"") + key + "\""); if (i < 0) return 0;
  return j.substring(j.indexOf(':', i) + 1).toInt();
}

// ── posting a finished load (with retry + flash-backed queue) ───────────────
static bool postJson(const String& body) {
  String resp; int code = apiCall("POST", "/api/mixer/load", body, resp);
  Serial.printf("[post] %d %s\n", code, resp.substring(0, 120).c_str());
  return code == 200;
}

static void queuePending(const String& body) { prefs.putString("pending", body); }
static void flushPending() {
  String body = prefs.getString("pending", "");
  if (body.length() == 0) return;
  if (postJson(body)) { prefs.remove("pending"); Serial.println("[post] pending load delivered"); }
}

static String buildLoadJson(time_t endedAt) {
  float avgC = load.n ? (float)(load.sumC / load.n) : NAN;
  float bv = readBatteryV(), bp = batteryPct(bv);
  String uid = String(TRUCK_LABEL) + "-" + (load.ref.length() ? load.ref : "noref") + "-" + String((long)load.startedAt);
  uid.replace(" ", "");
  String j = "{";
  j += "\"load_uid\":\"" + uid + "\",";
  j += "\"truck_id\":\"" + String(TRUCK_LABEL) + "\",";
  j += "\"started_at\":" + String((long)load.startedAt) + ",";
  j += "\"ended_at\":" + String((long)endedAt) + ",";
  if (!isnan(avgC)) { j += "\"mix_temp_c\":" + String(avgC, 1) + ",\"mix_temp_f\":" + String(avgC * 9.0f / 5.0f + 32.0f, 1) + ","; }
  if (bv >= 0)      { j += "\"batt_v\":" + String(bv, 2) + ",\"batt_pct\":" + String(bp, 0) + ","; }
  j += "\"fw\":\"" + String(FW_VERSION) + "\"}";
  return j;
}

static void finishLoad() {
  time_t endedAt = time(nullptr);
  String body = buildLoadJson(endedAt);
  Serial.printf("[load] ended %s: %d samples, avg %.1f °C (min %.1f / max %.1f)\n",
                load.ref.c_str(), load.n, load.n ? load.sumC / load.n : 0.0, load.minC, load.maxC);
  if (!postJson(body)) { queuePending(body); Serial.println("[post] failed — queued for retry"); }
  load = LoadStats();
}

// ── main ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nAussieblock mixer sensor fw %s — truck %s\n", FW_VERSION, TRUCK_LABEL);
  if (STATUS_LED_PIN >= 0) { pinMode(STATUS_LED_PIN, OUTPUT); led(false); }
  if (BATT_ADC_PIN >= 0) analogSetPinAttenuation(BATT_ADC_PIN, ADC_11db);
  probe.begin();
  Serial.printf("[probe] %d DS18B20 found on GPIO %d\n", probe.getDeviceCount(), ONEWIRE_PIN);
  prefs.begin("mixer", false);
  wifiUp(); syncClock();

#if BENCH_TEST
  // Prove the whole chain from the bench: post a fake 5-minute load, then remove it.
  load.active = true; load.ref = "BENCH"; load.startedAt = time(nullptr) - 300;
  for (int i = 0; i < 3; i++) { float c = readTempC(); if (!isnan(c)) { load.sumC += c; load.n++; load.minC = min(load.minC, c); load.maxC = max(load.maxC, c); } delay(800); }
  String body = buildLoadJson(time(nullptr));
  String uid = jsonStr(body, "load_uid");
  Serial.println("[bench] posting: " + body);
  if (postJson(body)) { String r; int code = apiCall("DELETE", "/api/mixer/readings/" + uid, "", r); Serial.printf("[bench] cleaned up: %d\n", code); }
  load = LoadStats();
#endif
}

void loop() {
  if (!wifiUp()) { delay(5000); return; }
  syncClock();
  flushPending();
  unsigned long now = millis();

  // Poll dispatch: is this truck loaded right now?
  if (now - lastPoll >= POLL_SECONDS * 1000UL || lastPoll == 0) {
    lastPoll = now;
    String resp; int code = apiCall("GET", "/api/mixer/active-load?truck=" + urlEncode(TRUCK_LABEL), "", resp);
    if (code == 200) {
      bool active = jsonBool(resp, "active");
      if (active && !load.active) {
        load = LoadStats(); load.active = true; load.ref = jsonStr(resp, "ref"); load.loadSeq = jsonInt(resp, "load_seq");
        load.startedAt = time(nullptr);
        Serial.printf("[load] started %s load %d (%s)\n", load.ref.c_str(), load.loadSeq, jsonStr(resp, "status").c_str());
        lastSample = 0;
      } else if (!active && load.active) {
        finishLoad();
      }
      led(load.active);
    } else {
      Serial.printf("[poll] failed (%d) — keeping current state\n", code);
    }
  }

  // Sample the drum while loaded.
  if (load.active && (now - lastSample >= SAMPLE_SECONDS * 1000UL || lastSample == 0)) {
    lastSample = now;
    float c = readTempC();
    if (isnan(c)) Serial.println("[probe] no reading");
    else { load.sumC += c; load.n++; if (c < load.minC) load.minC = c; if (c > load.maxC) load.maxC = c; Serial.printf("[probe] %.1f °C (%.1f °F)\n", c, c * 9 / 5 + 32); }
  }
  delay(200);
}
