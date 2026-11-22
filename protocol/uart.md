# Serial link: UNO ⇄ ESP32-CAM

The UNO drives the motors, the tilt servo and the sensors. The ESP32-CAM (or a PC on the
UNO's USB port, for bench tests) is the **host**. They talk over UART0 at **115200 baud, 8N1**.

The same wire also carries the ESP32's ROM boot messages, so the protocol must survive
random bytes. Every message is a framed, checksummed text line.

## Frame format

```
$<body>*<CC>\n
```

- `$` starts a frame. Anything received outside a frame is ignored. A `$` inside a frame
  throws away the partial frame and starts a new one.
- `<body>` is 1–64 printable ASCII characters (0x20–0x7E) other than `$` and `*`.
- `<CC>` is the CRC-8 of the body bytes as two hex digits (senders use uppercase,
  receivers accept either case). CRC-8 here is poly `0x07`, init `0x00`, not reflected,
  no final XOR (CRC-8/SMBUS). Check value: `crc8("123456789") = 0xF4`.
- `\n` ends the line. Receivers treat the frame as complete after the second CRC digit
  and ignore everything up to the next `$`, so `\r\n` works too.
- A frame with a bad CRC, an over-long body, or a malformed body is dropped silently and
  counted as a receive error.

The body is comma-separated fields:

```
TYPE,seq[,arg1[,arg2...]]
```

| Field | Rule |
|---|---|
| `TYPE` | 1–8 uppercase letters `A–Z` |
| `seq` | decimal `0`–`65535`, no sign, no leading `+`. Hosts number their requests `1`–`65535` (wrapping past `65535` back to `1`). `0` marks a message the UNO sends unprompted |
| args | at most 8. Integers are decimal with an optional leading `-` and fit in 32 bits. Some args are short words (`OBST`, `TIME`, `0.1.0`) |

Example: `$MOVE,12,60,0,0,800*C3\n`. The CRC values are in
[`uart_vectors.txt`](uart_vectors.txt), which both the C++ and Python implementations
are tested against.

## Host → UNO

| Message | Args | What the UNO does | Reply |
|---|---|---|---|
| `PING,seq` | – | Nothing, apart from resetting the link watchdog | `PONG,seq,<fw>,<uptime_s>` |
| `MOVE,seq,vx,vy,wz,ms` | `vx` forward(+)/back(−), `vy` right(+)/left(−), `wz` clockwise(+)/counter-clockwise(−), each `-100..100` percent of full speed. `ms` = `1..5000` | Starts a time-bounded mecanum move. A new `MOVE` or `TEST` replaces the running one | `ACK,seq`, later `DONE,seq,...` |
| `STOP,seq` | – | Stops the motors at once | `ACK,seq` (and `DONE,<running seq>,STOP,...` if something was running) |
| `TILT,seq,deg` | Camera tilt relative to level, positive = up. Clamped to the firmware's safe range | Moves the tilt servo | `ACK,seq,<applied deg>` |
| `CONF,seq,KEY,value` | `OBST` obstacle stop distance in cm, `5..100` (default 15). `MINPWM` lowest motor PWM that still turns the wheels, `0..255` (default 90). `LINKMS` link watchdog in ms, `300..5000` (default 1000) | Stores the setting in RAM. Settings reset to defaults at boot; the host re-sends them after `BOOT` | `ACK,seq` |
| `TEST,seq,bits,pwm5,pwm6,ms` | `bits` = raw 74HC595 byte `0..255`, `pwm5`/`pwm6` = raw PWM on D5/D6 `0..255`, `ms` = `1..2000` | Drives the shift register and PWM pins directly, for bench diagnostics (finding which PWM pin drives which wheels). No obstacle check | `ACK,seq`, later `DONE,seq,...` |

Out-of-range or malformed requests are rejected and change nothing:

| Reply | Meaning |
|---|---|
| `ERR,seq,TYPE` | Unknown message type |
| `ERR,seq,ARGS` | Wrong number of args, or an arg is not an integer where one is required |
| `ERR,seq,RANGE` | A value is outside its allowed range |

## UNO → host

| Message | When | Args |
|---|---|---|
| `BOOT,0,<fw>,<reset_flags>` | Once after reset, when the motors are confirmed off | Firmware version such as `0.1.0`, and the AVR `MCUSR` reset flags as a decimal number |
| `PONG,seq,<fw>,<uptime_s>` | Reply to `PING` | |
| `ACK,seq[,value]` | A request was accepted | `TILT` returns the applied angle |
| `ERR,seq,<code>` | A request was rejected | `TYPE`, `ARGS` or `RANGE` |
| `DONE,seq,<reason>,<cm_start>,<cm_end>,<ms_ran>` | A `MOVE` or `TEST` ended | Reason (below). Front distance in cm when it started and ended, `-1` if there was no echo. How long the motors actually ran |
| `TEL,0,<cm>,<line_l>,<line_c>,<line_r>,<tilt>,<move_seq>,<rx_errors>` | Every 200 ms | Front distance in cm (`-1` = no echo), the three line sensors as raw `0..1023` readings, current tilt in degrees, seq of the running move (`0` if none), and the receive error count |

`DONE` reasons:

| Reason | Meaning |
|---|---|
| `TIME` | Ran for the full `ms` |
| `OBST` | Stopped (or never started) because something was closer than the `OBST` distance ahead. Only checked while `vx > 0` |
| `STOP` | Stopped by a `STOP` request |
| `LINK` | No valid frame from the host for `LINKMS` ms, so the UNO assumed the host is gone |
| `NEW` | Replaced by a newer `MOVE` or `TEST` |

## Rules the UNO always enforces

- The motor driver outputs stay disabled from power-on until the shift register holds all
  zeros. Only then is `BOOT` sent.
- Every movement ends by itself after its `ms`.
- A forward move doesn't start, or stops, when the front distance is below `OBST`.
- While anything is moving, a silent host for `LINKMS` ms stops it. Hosts send `PING` at
  least every 300 ms.
- A frame that fails any check never moves anything.

## Mecanum mixing

Wheels: M1 front-left, M2 rear-left, M3 front-right, M4 rear-right. With `vy` positive
to the right and `wz` positive clockwise seen from above:

```
FL = vx + vy + wz        FR = vx - vy - wz
RL = vx - vy + wz        RR = vx + vy - wz
```

If any wheel exceeds 100 %, all four are scaled down together. The 74HC595 byte
(shifted MSB first) has these bits, where `+` is the direction that drives the car
forward:

| bit | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
|---|---|---|---|---|---|---|---|---|
| line | M1+ | M1− | M2+ | M2− | M4− | M3− | M3+ | M4+ |

Check values (all wheels at equal speed): forward `163`, back `92`, strafe left `106`,
strafe right `149`, forward-left `34`, forward-right `129`, back-left `72`,
back-right `20`, clockwise `172`, counter-clockwise `83`, stop `0`.

The shield has only two speed (PWM) pins for four wheels. On the AD176, D5 drives the
two right wheels and D6 the two left ones (measured on the bench with `TEST`). The
firmware gives each pin the duty of the fastest wheel on its side and switches off any
wheel below a quarter of that (`speed × 4 < fastest`, so exactly 25 % still drives).
That's exact for the pure moves listed above and for any mix of forward/back and
turning; a mix that includes strafing is approximate, because the two wheels on a side
then want different speeds. A switched-off wheel has both direction lines low while its
driver is enabled by its side's PWM, so the L293D brakes it rather than letting it spin
freely (the vendor's diagonal moves do the same). A build for a shield wired another
way can drive both pins with one shared duty instead (`SCOUT_PWM_SPLIT 0`).
