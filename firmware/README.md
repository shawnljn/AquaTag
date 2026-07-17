# Firmware

This directory will contain the embedded software for the Nordic nRF52832.

Planned contents:

- Reproducible toolchain and SDK setup
- Board definition and pin map
- BMI270 and MS5540 drivers/configuration
- Timestamped sensor acquisition at the paper's 100 Hz IMU setting
- NOR-flash buffering and FAT32 microSD logging
- Power management, charging state, and inactivity shutdown
- BLE services, commands, status, and above-water session transfer
- OTA update and SWD programming instructions
- Unit/hardware-in-the-loop tests and release binaries

Document the on-device log format and protocol compatibility before adding the application code. Secrets, production signing keys, and unique device identifiers must never be committed.
