# Security

Scout puts a camera on wheels inside someone's home and makes it reachable from the
internet, so security problems matter here.

## Reporting a problem

Please don't open a public issue. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include what you found, how to
reproduce it, and what an attacker could do with it.

## What counts

Anything that lets someone:

- see through the camera or drive the car without an activated account,
- get past the admin approval, become an admin, or act as another user,
- read or steal tokens, car keys or session cookies,
- take over a car through its WiFi setup page or a firmware update,
- make the car move when nobody asked it to, or keep moving after it should stop.

## How the system is meant to protect itself

- People sign in with GitHub, and new accounts can't reach the car until an admin
  activates them.
- AI clients use personal tokens. The server stores only their SHA-256 hashes, and any
  token can be revoked from the account page.
- Each car has its own secret key, also stored as a hash. The car always connects out to
  the server over TLS and checks the server's certificate.
- Firmware updates are checked against a SHA-256 hash before they are installed.
- Every movement is time-limited on the UNO itself, and the car stops when it loses its
  connection.

Secrets live in the hosting platform's settings or on the car, never in this repository.
