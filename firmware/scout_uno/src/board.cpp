#include "board.h"

#include <Arduino.h>
#include <avr/interrupt.h>
#include <avr/io.h>

#include "core/hardware.h"
#include "core/tilt.h"
#include "pins.h"

namespace {

const uint32_t kBaud = 115200;

// Timer1 runs the servo: fast PWM (mode 14, TOP = ICR1), clock / 8 = 0.5 us per tick, so
// 40000 ticks = 20 ms (50 Hz). No interrupts, so the pulse never jitters. Nothing else
// may use Timer1: no Servo library, no analogWrite() on D9 or D10, and no digitalWrite()
// or digitalRead() on D9 (the core would disconnect OC1A).
const uint16_t kServoTopTicks = 39999;
const uint16_t kServoMinUs = 500;
const uint16_t kServoMaxUs = 2500;

// Echo timing, shared with the pin-change interrupt.
enum EchoState { kEchoIdle, kEchoArmed, kEchoHigh, kEchoDone };
volatile uint8_t echoState = kEchoIdle;
volatile uint32_t echoRiseUs = 0;
volatile uint32_t echoWidthUs = 0;

void setServoCompare(uint16_t pulseUs) {
  if (pulseUs < kServoMinUs) pulseUs = kServoMinUs;
  if (pulseUs > kServoMaxUs) pulseUs = kServoMaxUs;
  OCR1A = static_cast<uint16_t>(pulseUs * 2 - 1);  // double-buffered: applies at the next period
}

void startServoTimer(uint16_t pulseUs) {
  TCCR1B = 0;  // stop the timer (the Arduino core set it up for 8-bit PWM)
  TCCR1A = 0;
  TIMSK1 = 0;
  TCNT1 = 0;
  ICR1 = kServoTopTicks;
  setServoCompare(pulseUs);
  DDRB |= _BV(DDB1);                             // D9 = OC1A as output
  TCCR1A = _BV(COM1A1) | _BV(WGM11);             // non-inverting on OC1A
  TCCR1B = _BV(WGM13) | _BV(WGM12) | _BV(CS11);  // mode 14, clk/8
}

}  // namespace

// D13 = PB5 = PCINT5, the only pin enabled in PCMSK0.
ISR(PCINT0_vect) {
  const uint32_t now = micros();
  if (PINB & _BV(PINB5)) {
    if (echoState == kEchoArmed) {
      echoRiseUs = now;
      echoState = kEchoHigh;
    }
  } else if (echoState == kEchoHigh) {
    echoWidthUs = now - echoRiseUs;
    echoState = kEchoDone;
  }
}

void boardBegin() {
  // OE# was already driven high in early_boot.cpp; doing it again costs nothing. HIGH
  // before OUTPUT, so the pin never drives low on the way.
  digitalWrite(kPinOutputEnable, HIGH);
  pinMode(kPinOutputEnable, OUTPUT);

  digitalWrite(kPinShiftData, LOW);
  digitalWrite(kPinShiftClock, LOW);
  digitalWrite(kPinLatch, LOW);
  pinMode(kPinShiftData, OUTPUT);
  pinMode(kPinShiftClock, OUTPUT);
  pinMode(kPinLatch, OUTPUT);

  digitalWrite(kPinPwm5, LOW);
  digitalWrite(kPinPwm6, LOW);
  pinMode(kPinPwm5, OUTPUT);
  pinMode(kPinPwm6, OUTPUT);

  digitalWrite(kPinTrigger, LOW);
  pinMode(kPinTrigger, OUTPUT);
  pinMode(kPinEcho, INPUT);

  startServoTimer(scout::tiltPulseUs(0));

  PCMSK0 = _BV(PCINT5);
  PCIFR = _BV(PCIF0);  // drop a stale flag
  PCICR |= _BV(PCIE0);

  Serial.begin(kBaud);
}

namespace scout {
namespace hw {

void setDriverEnabled(bool enabled) { digitalWrite(kPinOutputEnable, enabled ? LOW : HIGH); }

void writeShiftRegister(uint8_t bits) {
  for (uint8_t mask = 0x80; mask != 0; mask >>= 1) {  // MSB first
    digitalWrite(kPinShiftData, (bits & mask) ? HIGH : LOW);
    digitalWrite(kPinShiftClock, HIGH);
    digitalWrite(kPinShiftClock, LOW);
  }
  digitalWrite(kPinLatch, HIGH);  // rising edge copies the shift register to the outputs
  digitalWrite(kPinLatch, LOW);
}

void writePwm(uint8_t pwm5, uint8_t pwm6) {
  analogWrite(kPinPwm5, pwm5);  // 0 and 255 become plain LOW / HIGH
  analogWrite(kPinPwm6, pwm6);
}

void writeServo(uint16_t pulseUs) { setServoCompare(pulseUs); }

void triggerRanger() {
  const uint8_t sreg = SREG;
  cli();
  echoState = kEchoArmed;
  SREG = sreg;
  digitalWrite(kPinTrigger, HIGH);
  delayMicroseconds(10);  // the HC-SR04 wants a trigger pulse of at least 10 us
  digitalWrite(kPinTrigger, LOW);
}

bool takeEcho(uint32_t* widthUs) {
  bool ready = false;
  const uint8_t sreg = SREG;
  cli();
  if (echoState == kEchoDone) {
    *widthUs = echoWidthUs;
    echoState = kEchoIdle;
    ready = true;
  }
  SREG = sreg;
  return ready;
}

void readLine(uint16_t* a0, uint16_t* a1, uint16_t* a2) {
  *a0 = static_cast<uint16_t>(analogRead(A0));
  *a1 = static_cast<uint16_t>(analogRead(A1));
  *a2 = static_cast<uint16_t>(analogRead(A2));
}

void send(const char* data, uint8_t length) {
  Serial.write(reinterpret_cast<const uint8_t*>(data), length);
}

}  // namespace hw
}  // namespace scout
