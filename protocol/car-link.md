# Car link: ESP32-CAM ⇄ Scout server

The car opens one WebSocket to the server and keeps it open. The server never connects
to the car, which is what lets the car sit behind any home router.

```
wss://<server>/car/ws
```

Plain `ws://` is accepted only by a server started in development mode.

## Connecting

The upgrade request carries the car's secret key and firmware version:

```
Authorization: Bearer <car key>
X-Scout-Firmware: 0.1.0
```

- A missing or unknown key is refused before the upgrade completes (HTTP 403). The car
  waits and retries.
- A car has at most one connection. A new connection for the same car replaces the old
  one, which the server closes with code `4409`.
- Reconnect backoff on the car: 2 s, doubling, capped at 60 s, with ±20 % jitter. It
  resets after a connection has stayed up for 60 s.

Right after connecting the car sends `hello`. The server answers `welcome`, which
includes the settings the car should apply (it forwards the UNO ones as `CONF`).

## Messages

Control messages are JSON text frames with a `"t"` (type) field. Pictures are binary
frames. Requests from the server carry an `"id"`: a positive integer, unique for the
connection, that the car echoes in every reply to that request.

Unknown fields are ignored on both sides, so either side can add fields without
breaking the other. An unknown `"t"` from the server gets
`{"t":"err","id":<id>,"code":"unknown_type"}`.

### Server → car

| `t` | Fields | Car's replies |
|---|---|---|
| `welcome` | `car` (car id), `name`, `time` (unix seconds), `conf` (object, see `conf`) | none |
| `move` | `vx`, `vy`, `wz` (`-100..100`), `ms` (`1..5000`). Same meaning as the UART `MOVE` | `ack`, then `done` |
| `stop` | – | `ack` (plus `done` for a running move) |
| `tilt` | `deg`: camera tilt from level, positive = up | `ack` with `deg` = the applied angle |
| `light` | `level`: `0..100` | `ack` with the applied `level` |
| `snap` | `res`: one of `qvga` 320×240, `vga` 640×480, `svga` 800×600, `xga` 1024×768, `hd` 1280×720, `sxga` 1280×1024, `uxga` 1600×1200, `qxga` 2048×1536. `q`: JPEG quality `4..63`, lower is better, default 12 | one binary `SCIM` frame, or `err` |
| `stream` | `on` (bool). When `on`: `res`, `q`, `fps` (`1..10`), `secs` (`1..120`) | binary `SCST` frames until `secs` passes or a `stream` with `on:false` arrives. The server re-sends `stream` to keep it going. `ack` on both start and stop |
| `conf` | Any of `obst_cm` (`5..100`), `min_pwm` (`0..255`), `link_ms` (`300..5000`), `vflip` (bool), `hmirror` (bool) | `ack` once every value is applied |
| `test` | `bits` (`0..255`), `pwm5`, `pwm6` (`0..255`), `ms` (`1..2000`). Same as the UART `TEST`; admin-only on the server | `ack`, then `done` |
| `ota` | `url` (https), `sha256` (hex), `size` (bytes) | `ota` progress messages; the car reboots after a verified install |
| `reboot` | – | `ack`, then the car restarts |
| `ping` | – | `pong` |

### Car → server

| `t` | When | Fields |
|---|---|---|
| `hello` | First message on every connection | `fw`, `uno_fw` (`null` until the UNO answered), `camera` (`"OV3660"`, `"OV2640"`, … or `null`), `psram` (bytes, `0` if none), `max_res`, `mac`, `ip`, `ssid`, `rssi`, `reset` (reset reason) |
| `tel` | Every 1 s, and every 200 ms while a move runs | `cm` (front distance, `null` = no echo), `line` (`[l, c, r]` raw `0..1023`), `tilt`, `moving` (bool), `light`, `rssi`, `heap` (free bytes), `uno` (bool: UNO answering), `up` (uptime s) |
| `ack` | A request was accepted | `id`, plus request-specific fields (`deg`, `level`) |
| `done` | A `move` or `test` ended | `id`, `reason` (`time`, `obstacle`, `stopped`, `link`, `replaced`, `error`), `cm_start`, `cm_end` (`null` = no echo), `ms` (how long the motors ran) |
| `err` | A request failed | `id`, `code` (below), `msg` (short text for logs) |
| `pong` | Reply to `ping` | `id` |
| `ota` | During an update | `id`, `state` (`downloading`, `verifying`, `rebooting`, `failed`), `pct`, `msg` |
| `event` | Something changed on the car | `kind` (`uno_lost`, `uno_back`, `uno_boot`, `camera_error`), `msg` |

UART `DONE` reasons map to `done` reasons: `TIME`→`time`, `OBST`→`obstacle`,
`STOP`→`stopped`, `LINK`→`link`, `NEW`→`replaced`. The car sends `reason:"error"` (and
an `err` with code `timeout`) if the UNO never reports `DONE` within `ms + 1500` ms.

`err` codes: `uno` (UNO not answering), `camera`, `args` (missing or out-of-range field),
`busy` (for example `ota` while moving), `timeout`, `unknown_type`, `ota`.

### Binary frames

```
bytes 0-3   ASCII "SCIM" (snapshot) or "SCST" (stream frame)
bytes 4-7   request id, uint32 little-endian
bytes 8-    JPEG image
```

## Timing and safety

- The server sends `ping` every 5 s. The car reconnects if it hears nothing from the
  server for 15 s. The server drops a car it hears nothing from for 15 s.
- When the connection drops, the car stops the motors and switches the light off
  before it starts reconnecting.
- The car never moves except on a `move` or `test` request, and every one of those is
  time-bounded on the UNO.
- An `ota` request is refused with `busy` while a move runs.
- `conf` values live on the server and arrive in every `welcome`, so a replaced or
  reflashed car picks them up again.

## Example

```
car    → {"t":"hello","fw":"0.1.0","uno_fw":"0.1.0","camera":"OV3660","psram":4194304,"max_res":"qxga","mac":"02:00:00:00:00:01","ip":"192.168.1.40","ssid":"home","rssi":-58,"reset":"poweron"}
server → {"t":"welcome","car":"scout-1","name":"Scout","time":1795300000,"conf":{"obst_cm":15,"min_pwm":90,"link_ms":1000,"vflip":true,"hmirror":false}}
server → {"t":"snap","id":1,"res":"vga","q":12}
car    → <binary "SCIM" 01 00 00 00 FF D8 ... FF D9>
server → {"t":"move","id":2,"vx":60,"vy":0,"wz":0,"ms":800}
car    → {"t":"ack","id":2}
car    → {"t":"tel","cm":82,"line":[540,612,498],"tilt":0,"moving":true,"light":0,"rssi":-58,"heap":151204,"uno":true,"up":95}
car    → {"t":"done","id":2,"reason":"time","cm_start":95,"cm_end":71,"ms":800}
```
