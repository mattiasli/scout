# Simulator

`scout-sim` is a simulated Scout car: a mecanum car in a small room with a cat. It speaks
the car side of [the car link](../protocol/car-link.md) and follows the motor rules of
[the UART protocol](../protocol/uart.md), so the server can't tell it from a real car.
Use it to develop and test the server, the MCP tools and the web pages without hardware.
It never touches serial ports or the real car.

## Running it

From `server/`, with the server running:

```sh
uv run scout-sim --url ws://127.0.0.1:8000/car/ws --key KEY
```

`KEY` is a car key the server knows. The URL and key can also come from `SCOUT_SIM_URL`
and `SCOUT_CAR_KEY`. Against a server on another machine use its `wss://` address. Stop
it with Ctrl+C.

| Option | Meaning |
|---|---|
| `--url URL` | The server's car link (default `ws://127.0.0.1:8000/car/ws`, or `$SCOUT_SIM_URL`) |
| `--key KEY` | The car's secret key (or `$SCOUT_CAR_KEY`) |
| `--seed N` | Random seed for the cat, the sensor noise and the speed errors |
| `--fw VERSION` | Firmware version to report (default `0.1.0`) |
| `--camera NAME` | Camera sensor to report (default `OV3660`) |
| `--max-res RES` | Largest resolution the camera offers (default `qxga`) |
| `--no-cat` | Leave the cat out |
| `--still-cat` | The cat sits still |
| `--door-open` | The door starts open |
| `--pwm MODEL` | How the motor board drives its two PWM pins: `shared` (default), `split` or `ideal` (see [Wheels](#wheels)) |
| `--render FILE` | Write one camera frame from the start pose as a JPEG and exit (no server needed) |
| `--res RES` | Resolution for `--render` (default `vga`) |
| `-v` | Log more |

### Trying `follow`

The follow tool's detector (YOLOX-Nano, trained on COCO photos) doesn't recognise the
simulator's cartoon cat. To try `follow` end to end, start the **server** with
`SCOUT_FOLLOW_DETECTOR=sim-cat`. That swaps in a small colour detector that reports the
one bright orange thing in the room as a cat. Never set it for a real car.

## The room

A 400 × 300 cm room seen from above. `x` runs along the long wall and `y` across it,
both in cm from the corner; headings are counter-clockwise from `+x`.

| Thing | Where | Size |
|---|---|---|
| Sofa (blue) | x 310–390, y 240–290 | 45 cm high |
| Cabinet (green) | x 20–80, y 250–290 | 90 cm high |
| Cardboard box (grey) | x 180–220, y 30–70 | 35 cm high |
| Plant pot (grey-green) | centre (350, 60) | radius 14 cm, 40 cm high |
| Door | in the `x = 0` wall, y 110–190 | 200 cm high, closed unless `--door-open` |
| Cat (orange, striped) | starts at (290, 170) | radius 11 cm, 42 cm long, walks at 18 cm/s |
| Car | starts at (110, 150) facing `+x` | radius 12 cm for collisions |

The walls have different colours so you can tell which way the car faces. The floor is
a checkerboard, which shows motion in the pictures. The cat wanders: it sits for a while,
walks a few seconds with gentle turns, and turns away from anything it would bump into.

## The car

### Wheels

Moves go through the same mixing as the UNO firmware
(`firmware/scout_uno/src/core/mecanum.cpp`): `FL = vx+vy+wz`, `FR = vx−vy−wz`,
`RL = vx−vy+wz`, `RR = vx+vy−wz` in whole percent, all four scaled by 100/peak (rounded
toward zero) if one exceeds 100 %. Body motion then comes from mecanum forward
kinematics. At 100 % the car does 30 cm/s forward, 20 cm/s sideways
and 180°/s turning (less at lower percentages, see the motor curve below). Each move draws a speed error (3 % standard deviation), so dead
reckoning drifts the way it does on the real car. Walls, furniture and the cat block
the car.

The stock shield has only two PWM pins for four wheels. The simulator drives them the
way the firmware does, chosen with `--pwm`:

- `split` (the default, as the AD176 is wired; firmware `SCOUT_PWM_SPLIT 1`): each pin
  drives one side at the speed of that side's fastest wheel.
- `shared` (`SCOUT_PWM_SPLIT 0`): both pins carry the duty of the fastest wheel, so
  every driven wheel turns at that speed.
- `ideal`: every wheel at its own mixed speed, as if each had its own pin. The real
  shield can't do this.

With `split` and `shared`, a wheel below a quarter of the fastest wheel on its pin
coasts (the firmware's `kCoastDivisor`); exactly a quarter is still driven. Pure moves
(the eight directions and turning on the spot) come out exactly. With `split`, so does
any mix of driving and turning: `vx 50, wz 20` gives the left wheels 70 % and the right
ones 30 %, and the car curves. Mixes with strafing lose their small part, because the
two wheels on a side then want different speeds. With `shared` the same `vx 50, wz 20`
runs all four at 70 %, so the car goes straight, and `vx 40, wz 30` (70 and 10 %)
leaves the slow side coasting, so the car pivots.

Each wheel's percentage becomes a PWM duty the way the firmware maps it (1..100 % onto
`min_pwm`..255), and the motor turns at a speed proportional to the duty above 25
(`Physics.zero_speed_pwm`), as measured on the AD176. A duty below 100
(`Physics.start_pwm`) leaves the wheel still, because every move starts from a
standstill and the real motors only hum there. The server's estimates use the same
curve, without the stall.

`TEST` uses the raw bits and duties, with D5 driving the right wheels and D6 the left
ones as on the real car.

### Motor board rules

These are the UNO rules from the UART protocol:

- Every move and test ends by itself after `ms`.
- A new one replaces the running one (`replaced`), and `stop` ends it (`stopped`).
- A move with `vx > 0` doesn't start, or stops, while the front distance is below
  `obst_cm` (`obstacle`). Back, sideways and turning moves ignore the distance, and so
  does `TEST`.
- A running move stops after `link_ms` without a frame from the camera board (`link`).
- If the motor board never reports the end of a move, the camera board sends
  `done` with `error` and an `err` with code `timeout` after `ms` + 1.5 s.

### Sensors

- **Ultrasonic**: the nearest echo within ±7.5° of straight ahead, measured from 10 cm
  in front of the turning centre. It has ±0.8 cm of noise (`Physics.echo_noise_cm`),
  reads `null` beyond 400 cm and never reads below 2 cm. Walls, furniture and the cat
  all echo. A move that ends on an obstacle reports the reading that stopped it, as the
  UNO does.
- **Line sensors**: plain-floor readings with a little noise.
- **Camera**: a raycaster with a 66° field of view, 9 cm above the floor. Walls,
  furniture, the door and the cat are all distinguishable. The room light fades with
  distance and the light (LED) brightens what is close. Tilt runs from −40° to +40°,
  the firmware's limits; the car clamps a request outside them and reports the angle it
  applied. Every resolution in the protocol works up to `--max-res`. Frames wider than
  1024 px are rendered at 1024 and scaled up. JPEG quality follows the camera's `q`
  (lower is better). Until `conf` or `welcome` sets `vflip`, pictures are upside down,
  as on the real sensor. Capture takes about 40 ms for `qvga` and 450 ms for `qxga`,
  plus the rendering time.

### On the link

- `hello` is always the first message.
- `tel` arrives every second, and every 200 ms while a move runs.
- Every request in the protocol is handled, with `ack`, `done`, `err` (`args`, `busy`,
  `uno`, `camera`, `timeout`, `unknown_type`), `pong`, binary `SCIM` and `SCST` frames,
  `ota` progress and `event`s.
- `ota` doesn't download anything. It reports `downloading` 0–100 %, then `verifying`
  and `rebooting`, and then reconnects with the patch version bumped and reset reason
  `ota`. An `ota` while a move runs gets `busy`.
- If the server is silent for 15 s, the car reconnects. If the connection drops, it
  stops the motors and switches the light off first.
- Reconnect backoff: 2 s, doubling up to 60 s, ±20 % jitter, reset after 60 s connected.
- A refused key (HTTP 403) is retried with the same backoff.

## Faults

Tests inject trouble through `scout_sim.Faults`. Change the fields at any time and they
take effect at once:

| Field | Effect |
|---|---|
| `drop` | Request types whose replies never arrive, for example `{"snap"}` |
| `delay_s` | Extra reply delay per request type, `"*"` for all |
| `camera_error` | `snap` and `stream` fail with `camera` |
| `uno_offline` | The motor board doesn't answer: moves fail with `uno` and `hello` reports `uno_fw: null` |
| `uno_no_done` | The motor board never reports the end of a move |
| `uno_link_lost` | The camera board stops pinging the motor board, so running moves end with `link` |
| `silent` | The car sends nothing at all |
| `ota_fail` | The update fails verification |

`SimCar` also has `send_raw()` (send any frame, to test how the server copes with junk),
`drop_connection()` (cut the connection without a close frame, like a WiFi dropout) and
`restart()`.

```python
import asyncio

from scout_sim import Faults, SimCar, SimConfig, World

world = World.default(seed=1)
world.place_car(300, 150, 0)  # facing the far wall, 90 cm from the sensor
faults = Faults(delay_s={"snap": 2.0})
car = SimCar(SimConfig(url="ws://127.0.0.1:8000/car/ws", key="KEY"), world=world, faults=faults)
asyncio.run(car.run())  # in a test: car.start(), and later await car.close()
```

`World.empty()` gives just the four walls, for tests that need exact geometry. Give it
a `seed` and `Physics(echo_noise_cm=0)` when a test must decide the same way on every
run: the noise is drawn whenever a reading is taken, and telemetry timing changes how
many readings there are.
`server/tests/robot/conftest.py` runs the real `Robot` in a Starlette app on uvicorn with
simulated cars connecting over real WebSockets. The suite speeds the car up four times
and tells the server's calibration the same.

## What it doesn't model

- Wheel slip, other than the per-move speed error.
- Carpet and battery sag.
- WiFi latency and bandwidth.
- Camera exposure and blur.
- Anything on the line sensors.
- The UART byte framing between the two boards: the rules are simulated, the CRC frames
  are not.

The calibration speeds, the follow controller and the orbit geometry all need tuning on
the real car; the simulator only shows that they behave sensibly.
