# Architecture

## The parts

```
 Grok Bot / Claude Code / Codex          web browsers
            │  MCP over HTTPS                 │  HTTPS
            └───────────────┬─────────────────┘
                            ▼
          ┌────────────── Scout server (Railway) ──────────────┐
          │  website · GitHub sign-in · accounts · tokens       │
          │  MCP endpoint · driver lock · activity log          │
          │  robot: car link, actions, look-around, capture,    │
          │         follow                                      │
          │  SQLite + photos on a volume                        │
          └────────────────────────▲────────────────────────────┘
                                   │  WebSocket over TLS, opened by the car
                         ┌─────────┴─────────┐
                         │    ESP32-CAM      │  camera, WiFi, setup page, updates
                         └─────────┬─────────┘
                                   │  serial, 115200 baud, framed + CRC
                         ┌─────────┴─────────┐
                         │      UNO          │  motors, tilt, sensors, safety
                         └───────────────────┘
```

Each layer works at its own speed:

| Layer | Reacts in | Job |
|---|---|---|
| UNO | milliseconds | Runs each move for a fixed time, stops for obstacles ahead, stops when the link goes quiet, keeps the motors off at power-up |
| ESP32-CAM | tens of milliseconds | Takes pictures, keeps the connection to the server alive, translates between the server and the UNO |
| Server | tens of milliseconds | Checks who may do what, runs the faster routines (look-around, capture runs, following) |
| The AI | seconds | Decides what to do next, one step at a time |

The AI is the slowest and least reliable part, so nothing that needs good timing or
safety depends on it.

## Design rules

- **Every movement is bounded.** A move is "forward at 60 % for 800 ms", never "start
  driving". The UNO ends it by itself.
- **Every command reports what happened.** Moves come back with the reason they ended and
  the distance ahead before and after, because the AI can't feel the car.
- **The car dials out.** It opens one WebSocket to the server and keeps it open, so it
  works behind any home router and on any WiFi network it has been set up for.
- **The server is the only door.** AI clients and browsers never reach the car directly.
  The server checks the account, the token and the driver lock before anything reaches
  the car.
- **One driver at a time.** The first activated user to send a command holds the car
  until they release it or have been idle for two minutes. Admins can always stop the car
  or take over.

## Server

The server is a single Python program (Starlette + the MCP Python SDK) so one Railway
service with one volume runs everything. Inside, it has two halves joined by
[`server/src/scout/contracts.py`](../server/src/scout/contracts.py):

- **Robot side** (`scout.robot`): the car link, the tool actions and the behaviours. It
  knows nothing about users.
- **App side** (the rest of `scout`): the website, sign-in, accounts, tokens, the driver
  lock, the activity log and the MCP endpoint. It decides who may do what and then calls
  the robot side.

`scout_sim` is a simulated car that speaks the same protocol as the real one. The tests
use it, and it lets anyone work on the server without a car.

## Accounts

| Status | Can do |
|---|---|
| Waiting | Signed in with GitHub; no car access |
| Active | Everything with the car, the same as anyone else |
| Admin | Everything, plus approve or suspend users, grant admin, stop the car or take over, update firmware, see the activity log |

The owner's GitHub account (set by numeric id in the server settings) is always an admin.

## Protocols

- [UNO ⇄ ESP32 serial link](../protocol/uart.md)
- [ESP32 ⇄ server WebSocket link](../protocol/car-link.md)

## Testing

| What | How |
|---|---|
| Serial framing | Shared test vectors run against both the C++ and the Python implementation |
| UNO and ESP32 logic | Host-side C++ unit tests (no board needed) |
| Firmware builds | Compiled for both boards in CI |
| Server | Unit tests, plus end-to-end tests with the simulated car and a real MCP client |
| Real car | Bench tests from a PC over USB, with the wheels off the ground |

CI runs all of the automated tests on every push and pull request. Railway deploys `main`
only after CI has passed.
