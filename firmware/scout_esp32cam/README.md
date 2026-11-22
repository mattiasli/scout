# Scout ESP32-CAM firmware

The ESP32-CAM is the car's link to the outside world. This firmware:

- joins one of up to five saved WiFi networks and keeps a single WebSocket open to the
  Scout server ([protocol/car-link.md](../../protocol/car-link.md)). The car always
  dials out, so it works behind any home router;
- forwards `move`, `stop`, `tilt`, `test` and `conf` to the UNO over the serial link
  ([protocol/uart.md](../../protocol/uart.md)) and turns the UNO's answers into `ack`,
  `done` and `err` messages;
- PINGs the UNO every 250 ms and reports when it stops answering, comes back or restarts;
- takes JPEG pictures (`snap`) and short picture streams (`stream`);
- drives the white light on GPIO4 and the red status LED on GPIO33;
- installs firmware updates over HTTPS (`ota`) and rolls back if the new image can't
  reach the server;
- opens a setup access point with a web page when it has no usable WiFi.

UART0 (GPIO1/GPIO3) is the wire to the UNO, so the firmware never prints anything:
every log output of the core and of ESP-IDF is switched off at boot. Only the ROM boot
messages and a crash dump still appear, and the UART framing ignores them.

## Layout

| Path | What |
|---|---|
| `scout_esp32cam.ino` | Entry point only |
| `src/config.h` | Every tunable: version, timings, pins, limits |
| `src/app.cpp` | Boot, setup mode or car link, and every car-link request |
| `src/uno_bridge.*` | UART0 task: PINGs, parsing, telemetry |
| `src/link_client.*` | WebSocket client over TLS (or plain TCP for development) |
| `src/camera.*` | Sensor detection, frame buffers, snapshots |
| `src/leds.*` | White light (PWM) and status LED |
| `src/wifi_setup.*` | Joining saved networks; the setup access point and page |
| `src/settings.*` | Settings in NVS |
| `src/ota.*` | Firmware download, verification, rollback |
| `src/core/` | Logic with no hardware in it, unit-tested on the PC |

There are no third-party libraries. The WebSocket client is written from scratch
(`src/core/ws_*`), JSON parsing uses the cJSON copy that ships with ESP-IDF, and TLS
uses the core's `NetworkClientSecure` with the built-in CA bundle.

## Board settings

| Setting | Value |
|---|---|
| Core | esp32 by Espressif, 3.3.13 |
| Board | AI Thinker ESP32-CAM |
| Partition scheme | Minimal SPIFFS (1.9MB APP with OTA/128KB SPIFFS) |
| Everything else | Defaults (240 MHz, QIO 80 MHz flash, core debug level None) |

FQBN:

```
esp32:esp32:esp32cam:PartitionScheme=min_spiffs
```

Why these:

- The **AI Thinker ESP32-CAM** board definition always enables PSRAM, with the
  compiler flags for the ESP32's PSRAM cache bug, and its serial monitor leaves DTR/RTS
  alone. That matters on this board: its CH340C auto-reset circuit would otherwise hold
  the ESP32 in reset or in the bootloader while the monitor is open.
- **min_spiffs** is the only 4 MB scheme on this board with two app slots large enough
  for this firmware, which over-the-air updates need. The board's default, `huge_app`,
  has a single 3 MB slot and no OTA. The firmware uses no file system, so the small
  SPIFFS partition is simply unused.
- **Core debug level None** keeps the core's own log calls out of the binary. They would
  go to UART0, which belongs to the UNO.

## Building

From the repository root:

```
arduino-cli compile --fqbn esp32:esp32:esp32cam:PartitionScheme=min_spiffs --libraries firmware/libraries --warnings all --build-path build/esp32cam firmware/scout_esp32cam
```

`firmware/libraries` provides `ScoutLink`, the UART framing shared with the UNO firmware.
In the Arduino IDE, choose the board and partition scheme above and add
`firmware/libraries/ScoutLink` as a library (or put the repository's `firmware` folder
in your sketchbook).

The PC unit tests for `src/core/` are in `firmware/tests/esp32`:

```
cmake -S firmware/tests -B build/esp32-tests -G "MinGW Makefiles" -DCMAKE_CXX_COMPILER=g++
cmake --build build/esp32-tests
ctest --test-dir build/esp32-tests --output-on-failure
```

## Flashing over USB

1. **Unplug the cable between the ESP32-CAM and the UNO** (or lift the ESP32-CAM off
   the car). Both boards share UART0: with the cable in, the UNO's TX line fights the
   USB adapter and the upload fails, and the UNO would receive the whole upload as
   garbage on its serial port.
2. Plug the ESP32-CAM's USB port into the PC and note its COM port.
3. Upload:

   ```
   arduino-cli upload --fqbn esp32:esp32:esp32cam:PartitionScheme=min_spiffs -p COM7 --input-dir build/esp32cam firmware/scout_esp32cam
   ```

   The board resets itself into the bootloader through the CH340C. If the upload
   can't connect, hold the IO0 (BOOT) button, press and release RST, then release IO0
   and try again.
4. Unplug USB, plug the inter-board cable back in, switch the car on.

The first upload with this partition scheme also rewrites the partition table. Saved
settings (in NVS) survive later uploads; "Erase All Flash Before Sketch Upload" clears
them.

## First-time WiFi setup

You need a phone or laptop with WiFi, the Scout server's address and the car's key
(both come from the Scout website).

1. Switch the car on. With nothing saved yet it starts in setup mode: the red LED on
   the ESP32-CAM gives **two short blinks every two seconds**.
2. On your phone, open the WiFi settings and join the network called
   **Scout-Setup-XXXX** (XXXX is different for every board). The password is
   **scoutsetup**.
3. The setup page usually opens by itself. If it doesn't, open a browser and go to
   **http://192.168.4.1**.
4. Under **WiFi networks**, pick your home network from the list (or type its name if
   it's hidden), enter its password and press **Add network**. Only 2.4 GHz networks
   work. You can save up to five; the car tries the strongest one it can see first.
5. Under **Server**, enter the server address, for example `wss://scout.example.com/car/ws`,
   and press **Save address**.
6. Under **Car key**, paste the key and press **Save key**. The page only ever shows
   whether a key is set, never the key itself.
7. Optional but recommended: under **Setup password**, choose your own password for the
   setup network.
8. Press **Save and restart**. The car restarts and joins your WiFi. Reconnect your
   phone to your usual network.

While it connects the LED blinks quickly; once the server has answered it stays on.

## When the car enters setup mode

- No WiFi network, server address or car key is saved.
- None of the saved networks could be joined within about 30 seconds of switching on.
- The reset button was pressed, or the power switched off and on, three times within
  about ten seconds of running time (press, wait a moment, press again). Use this to
  change settings when the car is connected and working.

In setup mode the motors stay stopped and the camera is off. If nobody is connected to
the setup network, the car tries the saved networks again every three minutes and
restarts normally as soon as one works, so a router that was off at power-up doesn't
leave the car stuck in setup mode.

If WiFi drops later, the car does not go into setup mode. It stops the motors, switches
the light off, and keeps trying: the same network straight away, all saved networks
every two minutes.

## Status LED (red, GPIO33)

| Pattern | Meaning |
|---|---|
| Two short blinks every 2 s | Setup mode |
| Fast blinking (4 per second) | Joining WiFi or connecting to the server |
| Steady on | Connected to the server |
| Very fast flicker (10 per second) | Firmware update running |

## Firmware updates (OTA)

The server sends `ota` with an `https` URL, the image's SHA-256 and its size. The car:

1. refuses with `busy` while a move is running, and with `ota` if the URL, size or
   free memory is wrong;
2. downloads the image (redirects are followed, the certificate is checked against the
   built-in CA bundle) and writes it into the spare app slot while hashing it, sending
   `ota` progress messages every 10 %;
3. checks the size, the SHA-256 and the image's own checksum, and only then makes it the
   boot image;
4. sends `rebooting` and restarts.

The new image starts as "pending". It becomes permanent once it has connected to the
server and received `welcome`. If it resets before that, or hasn't reached the server
within 10 minutes, the bootloader goes back to the previous image. A power cut or reset
in the first minutes after an update therefore also rolls it back; the server can simply
send the update again.

The image to serve is `build/esp32cam/scout_esp32cam.ino.bin`.

## Checks on the real car

These couldn't be tested without the hardware:

- **Camera**: `hello` reports the sensor (expected OV3660, maybe OV2640) and `max_res`.
  Take a `snap` at every resolution up to `max_res` and at `q` 4 and 63. Check the
  picture isn't upside down or mirrored with the default `vflip` on, `hmirror` off.
- **PSRAM**: `hello` should report `psram` around 4 MB. With 0, pictures are limited to
  VGA.
- **Light**: `light` 0, 50 and 100 work, the LED doesn't get hot at 100 over a minute,
  and taking pictures with the light on still works (the camera clock and the light
  share the LEDC peripheral).
- **Brownouts**: snapshots at high resolution with the light at 100 and the motors
  running shouldn't reset the board (`hello`'s `reset` would say `brownout`).
- **UART**: with the UNO connected, `tel` shows `uno: true` and live `cm`/`line`; `move`
  gets `ack` and `done`; unplugging the UNO's power gives `uno_lost`, and plugging it
  back gives `uno_back` and `uno_boot`.
- **No stray output**: watch UART0 with a USB adapter during boot, WiFi connection,
  setup mode and an OTA: apart from the ROM boot text, only `$...*CC` frames.
- **Link watchdog**: a long `move` keeps going while a big picture is being sent over
  weak WiFi (the PING task must not stall).
- **Reset counter**: three presses of RST within 10 s enter setup mode, and so do three
  quick power cycles. The count is kept in RTC memory with a copy in NVS, because on the
  ESP32 the RST button may clear RTC memory; check that both ways work and that a
  single reset or a crash never enters setup mode.
- **Setup page**: the captive page opens by itself on Android and iOS; adding and
  deleting networks, saving the address and key, changing the setup password.
- **OTA**: an update from a real HTTPS URL with a redirect (e.g. a GitHub release)
  installs, and a wrong `sha256` is refused without changing anything.
