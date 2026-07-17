# Mobile app

The companion application described in the paper is built with React Native and uses BLE when AquaTag is above water.

Planned release contents:

- React Native project and locked dependencies
- Android development/build instructions and supported versions
- BLE permission and device-discovery setup
- AquaTag service/characteristic protocol
- Session configuration for pool length and mount location
- Session transfer and export
- Test/mocked-device workflow
- Screenshots and accessibility notes

Continuous underwater BLE connectivity is not assumed. Keep post-session/offline behavior available when radio communication is interrupted.
