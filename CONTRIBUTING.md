# Contributing

Thanks for helping with Scout. A few things keep the project working:

## Protocols are contracts

The car is built from parts written separately: the UNO firmware, the ESP32-CAM firmware,
the server and the simulated car. They only agree because of the documents in
[`protocol/`](protocol). If you change how two parts talk, change the protocol document,
both sides and their tests in the same pull request.

The serial test vectors are generated. Edit `tools/make_uart_vectors.py`, then run
`python tools/make_uart_vectors.py`.

## Before you open a pull request

```
# firmware unit tests
cmake -S firmware/tests -B build/firmware-tests
cmake --build build/firmware-tests
ctest --test-dir build/firmware-tests --output-on-failure

# server
cd server
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest

# tools
uv run --no-project --with pytest --with pyserial python -m pytest tools/tests
```

CI runs the same checks, builds both firmwares, and builds the server image.

## Safety

Changes that affect how the car moves need extra care. Every movement must stay
time-limited on the UNO, the car must stop when its link goes quiet, and the motors must
stay off at power-up. Test motion changes on a stand with the wheels off the ground
before trying them on the floor.

## Don't commit

- Secrets of any kind: tokens, car keys, WiFi passwords, server settings.
- Photos or recordings from inside anyone's home.
- Files from the car kit's vendor download. Scout's code is written from scratch.
