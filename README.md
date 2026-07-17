# Pulse-Eight Matrix Audio — Home Assistant Integration

A custom Home Assistant integration for controlling Pulse-Eight **ProAudio**
matrix audio switchers over TCP/IP, using the Universal Serial Protocol (V2.1).

## Features

Per output zone, the integration exposes:

- **`media_player`** — source selection, volume, and mute in one entity.
- **`select`** — the input routed to each output.
- **`switch`** — a mute toggle per output.

Inputs (analog RCA, coax and optical) are presented as named sources, mapped to
the switch's Extended I/O source numbers. Each input can be given a friendly
name (e.g. "Turntable", "Apple TV") from the integration's options.

## Supported models

All seven ProAudio models are supported, with input/zone counts taken from the
published specifications:

| Model | Analog / Coax / Optical inputs | Zones |
|---|---|---|
| ProAudio 8 | 8 / 8 / 8 | 8 |
| ProAudio 16 | 16 / 16 / 16 | 16 |
| ProAudio 16 RS | 16 / 16 / 8 | 16 |
| ProAudio 1632 | 16 / 16 / 8 | 32 |
| ProAudio 32 | 32 / 32 / 16 | 32 |
| ProAudio 3248 | 32 / 32 / 16 | 48 |
| ProAudio 3264 | 32 / 32 / 16 | 64 |

> The ProAudio 16 RS also has 8 RJ45 remote-source inputs. These are not yet
> exposed, as the V2.1 protocol guide does not document a source-number mapping
> for them.

## Installation (HACS)

1. In HACS, add `https://github.com/tsukasa-sama/pulse-eight-matrix-audio` as a
   **custom repository** (category: *Integration*).
2. Install **Pulse-Eight Matrix Audio**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *Pulse-Eight Matrix Audio*.
5. Enter the matrix **host/IP**, **port** (50005 by default), select your
   **model**, and leave **Extended I/O mode** enabled.

## Manual installation

Copy `custom_components/pulse_eight_matrix_audio/` into your Home Assistant
`config/custom_components/` directory and restart.

## Renaming inputs

**Settings → Devices & Services → Pulse-Eight Matrix Audio → Configure** opens a
form listing every input; set a friendly name for any of them. Names update
across all source selectors immediately.
