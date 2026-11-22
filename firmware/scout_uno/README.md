# Scout UNO firmware

Firmware for the Arduino UNO on the KUONGSHUN AD176 mecanum car. The UNO handles the
reflexes: the four wheel motors, the camera tilt servo, the ultrasonic sensor and the line
sensors, plus the safety rules below. It takes orders over its serial port from the
ESP32-CAM, or from a PC on the USB port for bench tests. The protocol is
[`protocol/uart.md`](../../protocol/uart.md); this firmware implements the UNO side of it.

Version 0.1.0.

## What it does

- Answers `PING`, `MOVE`, `STOP`, `TILT`, `CONF` and `TEST`, and sends `BOOT` once after
  reset, `ACK`/`ERR` for every request, `DONE` when a move ends, and `TEL` every 200 ms.
- Mixes `MOVE vx, vy, wz` into the 74HC595 direction byte and the two speed PWMs (D5, D6).
- Pings the HC-SR04 every 60 ms without blocking; the echo is timed by a pin-change
  interrupt on D13.
- Drives the tilt servo from Timer1 hardware PWM (50 Hz on D9): no interrupts and no
  jitter. Timer0 (`millis()` and the D5/D6 PWM) is untouched.

## Safety rules

These hold whatever the host sends:

- **Motors off at boot.** The first code after reset (`src/early_boot.cpp`, in `.init3`)
  drives the 74HC595 output enable (D7) high. The shield's 10k pull-up already keeps the
  outputs disabled while the pin floats. `setup()` then shifts zeros into the register,
  latches them, sets both PWMs to 0, and only then enables the outputs and sends `BOOT`.
- **Every move ends by itself.** `MOVE` runs at most 5000 ms and `TEST` at most 2000 ms.
- **Obstacle stop.** A `MOVE` with `vx > 0` doesn't start, or stops, when the front
  distance is below `OBST` (default 15 cm). No echo (`-1`) counts as "nothing ahead".
- **Link watchdog.** While anything moves, no valid request from the host for `LINKMS`
  (default 1000 ms) stops it with `DONE ... LINK`. Hosts send `PING` every 250-300 ms.
- **Bad frames move nothing.** A frame that fails the CRC or any other check is dropped
  and counted; a request with bad arguments gets `ERR` and changes nothing.
- **AVR watchdog.** If `loop()` stops running for 250 ms, the chip resets and the boot
  sequence switches the motors off. `MCUSR` is read and cleared and the watchdog is
  switched off at the very start, so a watchdog reset can't turn into a reset loop.

## Behaviour the spec leaves open

- **Reply order.** The motors change first. Then the reply to the request goes out
  (`ACK`/`ERR`), then any `DONE` it caused. A `MOVE` that replaces a running move sends
  `ACK,new`, then `DONE,old,NEW,...`, and if a forward move is blocked from the start,
  `DONE,new,OBST,cm,cm,0`. `STOP` sends `ACK`, then `DONE,running,STOP,...`.
- **Seq 0.** Seq 0 marks the UNO's own unprompted messages, so a request with seq 0 is
  ignored: no reply, and it doesn't feed the link watchdog. This also stops a TX line
  looped back to RX from keeping the watchdog alive with the UNO's own telemetry.
- **Which frames feed the link watchdog.** Every valid frame with a non-zero seq, even
  one that gets `ERR` (the host is clearly there).
- **Unknown `CONF` key** gets `ERR,seq,ARGS`.
- **Extra arguments** (`PING,1,0`, `STOP,2,1`) get `ERR,seq,ARGS`, and that `STOP` does
  not stop anything, following "rejected requests change nothing".
- **Ultrasonic readings.** A ping with no echo by the next ping (60 ms) gives `-1`. An
  echo longer than 400 cm is discarded and the last reading stays, but only for 200 ms;
  after that, with nothing plausible in between, the reading becomes `-1`.
- **`ms_ran` in `DONE`** is the real time between motors on and motors off (it can be a
  millisecond over `ms`).
- **Reset flags.** `BOOT` reports `MCUSR` as read at reset. The stock UNO bootloader
  (optiboot 4.4) clears `MCUSR` before the sketch starts, so on a stock UNO this is
  usually `0`, even after a watchdog reset.

## PWM modes

The shield has two speed pins for four wheels, and which wheels each pin drives hasn't
been measured yet. `src/core/config.h` selects the mode at compile time:

- `SCOUT_PWM_SPLIT 0` (the default): **shared**. Both pins carry the duty of the fastest
  wheel. A wheel below 25 % of the fastest is not driven (both of its lines low). That's
  exact for the pure moves in the spec and approximate for blended ones. Because the
  speed pin also enables that wheel's driver, a wheel that isn't driven during a move is
  probably braked rather than left to spin freely (L293D: both inputs low with the enable
  high shorts the motor). After `STOP` both PWMs are 0, so all wheels spin freely.
- `SCOUT_PWM_SPLIT 1`: **per side**. Each pin drives one side, at the duty of that
  side's fastest wheel; `SCOUT_PWM5_LEFT` says whether D5 is the left side (1) or the
  right (0). The 25 % rule then applies within each side. For other groupings, set
  `kPwmLayout` in `src/core/mecanum.h`.

Speed percent to PWM: 0 stays 0, and 1..100 maps linearly onto `MINPWM`..255.

## Code layout

```
scout_uno.ino          setup() and loop(); the watchdog
src/early_boot.cpp     .init3: OE# high, MCUSR saved and cleared, watchdog off
src/board.cpp          the hardware: pins, 74HC595, PWM, Timer1 servo, echo interrupt, ADC, serial
src/pins.h             the shield's pin map
src/core/              board-independent logic, no Arduino headers (built by the host tests too)
  scout.*              requests, the move/test state machine, obstacle stop, link watchdog, telemetry
  mecanum.*            mixing, PWM mapping, the 25 % rule
  ranger.*             ultrasonic timing and filtering
  tilt.*               tilt clamp (-40..+40 degrees) and servo pulse
  hardware.h           the functions the core calls; board.cpp and the test fake define them
  config.h             limits, defaults and the PWM mode
```

Pins used: D0/D1 serial, D2/D4/D7/D8 74HC595, D5/D6 PWM, D9 servo, D12/D13 ultrasonic,
A0-A2 line sensors. D10 and D11 are free. A3 is kept free for battery sensing, and A4/A5
for a gyroscope. Timer1 belongs to the servo, so don't add the Servo library or use
`analogWrite()` on D9/D10.

## Building

Host unit tests (no board needed), from the repository root:

```
cmake -S firmware/tests -B build/uno-tests -G "MinGW Makefiles" -DCMAKE_CXX_COMPILER=g++
cmake --build build/uno-tests
ctest --test-dir build/uno-tests --output-on-failure
```

On Linux or macOS, leave out `-G` and `-DCMAKE_CXX_COMPILER`.

Compile for the board with arduino-cli (the IDE ships a copy; `arduino:avr` must be
installed):

```
arduino-cli compile --fqbn arduino:avr:uno --libraries firmware/libraries --warnings all \
  --build-path build/uno-sketch firmware/scout_uno
```

Version 0.1.0 uses about 8.6 KB of flash (26 %) and 467 bytes of RAM (22 %).

## Flashing

**Unplug the cable between the UNO and the ESP32-CAM first.** The UNO's serial pins
are shared between USB and that link, and the ESP32 would talk over the upload.

With arduino-cli (the UNO is COM8 here):

```
arduino-cli compile --fqbn arduino:avr:uno --libraries firmware/libraries \
  --upload -p COM8 firmware/scout_uno
```

With the Arduino IDE: the sketch needs the `ScoutLink` library from
`firmware/libraries`. Either set File > Preferences > Sketchbook location to the
repository's `firmware` folder, or copy `firmware/libraries/ScoutLink` into the
`libraries` folder of your sketchbook. Then open `firmware/scout_uno/scout_uno.ino`, pick
Arduino Uno and the port, and upload.

After flashing, check it with `uv run tools/bench.py --port COM8 ping`, then plug the
ESP32 cable back in.

## Bench tool

[`tools/bench.py`](../../tools/bench.py) talks to the UNO over USB (keep the ESP32 cable
unplugged). Opening the port resets the UNO, so the tool waits for `BOOT` first and then
sends a `PING` every 250 ms while it runs. Ctrl+C always sends `STOP` before it exits.

```
uv run tools/bench.py --port COM8 ping
uv run tools/bench.py --port COM8 monitor                  # telemetry until Ctrl+C
uv run tools/bench.py --port COM8 move forward 40 800      # direction, wheel speed %, ms
uv run tools/bench.py --port COM8 drive 30 0 -20 1000      # vx vy wz ms
uv run tools/bench.py --port COM8 stop
uv run tools/bench.py --port COM8 tilt -20
uv run tools/bench.py --port COM8 raw 163 150 0 500        # TEST: 595 byte, D5, D6, ms
uv run tools/bench.py --port COM8 --conf MINPWM=110 move cw 30 1000
```

Directions: `forward`, `back`, `left`, `right`, `forward_left`, `forward_right`,
`back_left`, `back_right`, `cw`, `ccw`. Settings last only until the UNO resets (and
every run of the tool resets it), so pass them with `--conf KEY=VALUE`.

Guided tests. Each one starts with a warning and asks you to confirm that the car is on
a stand with its wheels off the ground:

- `wheels` drives each 74HC595 line alone (each wheel forward, then back), asks which
  wheel turned and which way, and prints a table that checks the bit map.
- `pwmmap` drives all wheels forward with only D5 on, then only D6, asks which wheels
  turned, and says which PWM mode to compile in.
- `obstacle` runs a slow forward move while you hold a hand in front of the sensor
  (it must never start), then another while you bring the hand in (it must stop).

## Still to measure on the car

- [ ] `wheels`: the bit map in `protocol/uart.md` matches the real motor wiring.
- [ ] `pwmmap`: which wheels D5 and D6 drive. Switch to per-side PWM if each is one side.
- [ ] `MINPWM`: the lowest PWM that still turns every wheel on the floor (default 90).
- [ ] Tilt: the real mechanical limits of the camera mount. The firmware allows -40..+40
      degrees; widen `kTiltMinDeg`/`kTiltMaxDeg` only after checking. Check also that
      positive really tilts up and that 0 is level.
- [ ] Line sensors: which of A0/A1/A2 is left, centre and right (the vendor sketches
      disagree), and what readings mean "line".
- [ ] Ultrasonic: readings against a tape measure, and what the module does with no
      echo (a long pulse, which is discarded, or no pulse, which gives `-1`).
- [ ] `obstacle`: the forward move stops for a hand, and the default 15 cm suits the car's
      stopping distance at full speed.
- [ ] Motors stay still while the UNO is in reset or being flashed (all UNO pins float
      then; the shield's pull-up on OE# should keep the drivers off).
- [ ] Reset flags: what `BOOT` reports after power-on and after a watchdog reset with
      this board's bootloader.
- [ ] Brown-out: the UNO doesn't reset when all four motors start at full speed
      (the bench tool warns if a `BOOT` arrives mid-session).
