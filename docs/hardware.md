# Hardware

Scout runs on the KUONGSHUN AD176 "ESP32 WiFi Camera Smart Robot Car" kit with mecanum
wheels. Everything here comes from inspecting the boards and from testing. Items marked
*unverified* still need measuring on a real car.

![All the parts of the AD176 kit laid out on a table](images/kit-parts.jpg)

## Boards

| Board | What it is | Role in Scout |
|---|---|---|
| Arduino UNO clone | ATmega328P, CH340C USB, USB-C | Motors, tilt servo, distance and line sensors, safety stops |
| UNO-Cam-Shield-V01 | 74HC595 shift register, two L293DD motor drivers, LM1085 5 V regulator | Sits on the UNO and powers everything from two 18650 cells |
| ESP32-CAM-V2.0 | ESP-32S module (4 MB flash), CH340C USB, USB-C, auto-reset | Camera, WiFi, link to the server |
| Camera | Ribbon marked "XY0033-3660-B", most likely an OV3660 (*unverified*) | Forward-facing camera |
| PSRAM | SOP-8 chip marked "6404H", most likely 8 MB PSRAM (*unverified*) | Frame buffers for larger images |

The UNO and the ESP32-CAM talk over a 4-wire cable (VCC, GND, RX, TX) at 115200 baud.
Both boards' USB ports share those same serial lines, so **unplug that cable before
flashing either board**.

## UNO pins (through the shield)

| Pin | Use |
|---|---|
| D2 | 74HC595 shift clock |
| D4 | 74HC595 latch |
| D7 | 74HC595 output enable, active low. A 10 kΩ pull-up keeps the motors off until the firmware drives it low |
| D8 | 74HC595 data |
| D5, D6 | Motor speed (PWM): D5 the two right wheels, D6 the two left ones (measured) |
| D9 | Camera tilt servo (KS-9GMS, SG90 class). Level is a servo angle of 81°; at 90° the camera points about 9° up |
| D12, D13 | Ultrasonic sensor trigger and echo (forward-facing) |
| A0, A1, A2 | Three downward-facing line sensors (which side is which is *unverified*) |
| D10, D11 | Free |
| A3, A4, A5 | Free, reserved: A3 for battery sensing, A4/A5 (I²C) for a gyroscope |

## Motors

Four geared DC motors with mecanum wheels: M1 front-left, M2 rear-left, M3 front-right,
M4 rear-right. The shift-register bit map and the mixing formula are in
[protocol/uart.md](../protocol/uart.md).

## ESP32-CAM pins

| GPIO | Use |
|---|---|
| 1, 3 | Serial link to the UNO (and USB) |
| 4 | White flash LED |
| 33 | Small red status LED (active low) |
| 0, 5, 18, 19, 21, 22, 23, 25, 26, 27, 32, 34, 35, 36, 39 | Camera |

## Power

Two 18650 Li-ion cells (not included in the kit) in the holder on the shield. The kit has
no battery-level sensing; adding it takes two resistors and pin A3, which is kept free.

![The AD176 box with two 18650 cells and a USB charger](images/kit-box.jpg)

## Known limits

- The camera and the distance sensor both face forward. Sideways and backward moves are
  blind.
- There are no wheel encoders and no gyroscope, so distances and turn angles are
  estimates. Mecanum wheels slip, especially sideways.
- The camera sits roughly 10–15 cm above the floor.
- Two speed channels for four wheels means blended moves (for example forward while
  turning) are approximate.
