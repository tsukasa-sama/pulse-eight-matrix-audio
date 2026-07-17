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

## Troubleshooting

The integration logs its whole lifecycle. INFO-level breadcrumbs appear by
default; enable DEBUG for the full protocol exchange.

```yaml
# configuration.yaml — then restart Home Assistant
logger:
  logs:
    custom_components.pulse_eight_matrix_audio: debug
```

What you'll see (all under `custom_components.pulse_eight_matrix_audio.*`):

| Logger | Level | Shows |
|---|---|---|
| `__init__` | INFO | Setup start (model, host, zones) and the version the switch reports |
| `config_flow` | WARNING | Why a connection attempt during setup failed |
| `coordinator` | DEBUG | Each poll and the routes/mutes/volumes returned |
| `client` | DEBUG | `Connecting to …`, `TX ^…$` / `RX ^…$` (raw frames), `Set route/mute/volume …` |
| `select` / `media_player` / `switch` | DEBUG | Each UI action, the target zone, and the resolved source number |

Reading a failure:

- **`Cannot connect to …`** — TCP connect failed/timed out. The switch services
  only a few sockets and holds them open for up to 10 minutes; if a prior
  connection is stuck, power-cycle the matrix to free it.
- **`No response … the switch sent nothing back`** — connected, but the command
  got no reply. Check the `TX`/`RX` lines to see how far it got.
- **`Error N: …`** — the switch rejected a command (see the protocol guide's
  error table).

The connection model is **connect-per-command** (open → send → close), so the
matrix never holds an idle Home Assistant socket and nothing leaks across
restarts.
