# Cudy Router HA Control

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-blue.svg)](https://github.com/cash83/cudy-router-ha-control)
[![Version](https://img.shields.io/github/v/release/cash83/cudy-router-ha-control?label=version)](https://github.com/cash83/cudy-router-ha-control/releases)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-green.svg)](LICENSE.md)

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=cash83&repository=cudy-router-ha-control&category=integration" target="_blank" rel="noopener noreferrer">
  <img alt="Open your Home Assistant instance and open this repository in HACS" src="https://my.home-assistant.io/badges/hacs_repository.svg">
</a>

Home Assistant custom integration for Cudy routers with a LuCI-based web interface.

The integration talks directly to the router on the local network and exposes router status, Wi-Fi, VPN, clients, SMS, reboot actions, switches, and select controls in Home Assistant.

This project is not endorsed, maintained, or supported by Cudy.

## Tested Focus

This build includes live fixes tested on:

- Cudy P4 / P4 5G firmware family
- Cudy M3000 firmware family

The main compatibility work in this branch focuses on reliable switch and select writes, ZeroTier VPN controls, P4 generic VPN pages, M3000 dedicated ZeroTier pages, Wi-Fi settings, and avoiding temporary state bounce in Home Assistant after a command is sent.

Other Cudy models may work when their firmware exposes compatible LuCI pages, but support depends on the exact model and firmware.

## Features

- Local polling through Home Assistant config flow.
- Sensors for WAN, LAN, modem/cellular, DHCP, Wi-Fi, VPN, traffic, system status, clients, and SMS-capable routers.
- Switch and select entities for writable router settings when the router exposes those controls.
- Improved P4 and M3000 VPN handling, including ZeroTier fields that differ between firmware families.
- Reboot buttons and services for router restart, 5G connection restart, band switching, SMS, and AT commands.
- Mesh devices connected sensor on routers that report mesh topology.
- Connected-client controls and optional device tracker entities.
- HACS custom repository support.

## Installation

### HACS

Use the button above, or add this repository manually:

1. Open **HACS** in Home Assistant.
2. Open the menu in the top-right corner.
3. Choose **Custom repositories**.
4. Add `https://github.com/cash83/cudy-router-ha-control` as an **Integration** repository.
5. Search for **Cudy Router** and install it.
6. Restart Home Assistant.

### Manual

1. Copy `custom_components/cudy_router` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings > Devices & Services**.
2. Add the **Cudy Router** integration.
3. Enter the router IP address, username, and password.

The integration normalizes the host automatically. For example, `192.168.10.1` is detected as either `http://192.168.10.1` or `https://192.168.10.1`.

## Services

The integration provides these Home Assistant services:

- `cudy_router.reboot_router`
- `cudy_router.restart_5g_connection`
- `cudy_router.switch_5g_band`
- `cudy_router.send_sms`
- `cudy_router.send_at_command`

Use the optional `entry_id` field when multiple Cudy routers are configured.

## Troubleshooting

If a switch or select appears to change and then returns to the old value, wait a few seconds after sending the command and refresh the device. Some Cudy firmware applies settings asynchronously after the LuCI form is submitted.

For router-specific problems, open an issue and include:

- Home Assistant version
- Integration version
- Router model and firmware
- The entity that failed
- A short description of what changed in the Cudy web page after the command

Bug reports: https://github.com/cash83/cudy-router-ha-control/issues/new?template=bug_report.yml

## Credits

This repository is based on the GPLv3 Home Assistant Cudy Router integration originally published by usersaynoso. This fork keeps the GPLv3 license and continues from that work with additional compatibility fixes tested on P4/P4 5G and M3000 routers.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE.md).
