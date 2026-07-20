"""Constants for the Pulse-Eight Matrix Audio integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "pulse_eight_matrix_audio"

# --- Connection ------------------------------------------------------------
# The TCP/IP control socket is fixed to 50005 on all ProAudio units
# (Universal Serial Protocol V2.1, "TCP/IP Settings Used By The Switch").
DEFAULT_PORT: Final = 50005
DEFAULT_TIMEOUT: Final = 5.0  # seconds for connect / per-command response

# --- Matrix size / model ---------------------------------------------------
CONF_MODEL: Final = "model"
CONF_EXTENDED_IO: Final = "extended_io"
# Options-flow key: {source key -> custom name}, e.g. {"analog_1": "Turntable"}.
CONF_SOURCE_NAMES: Final = "source_names"
# Options-flow key: list of source keys hidden from the zone source dropdown,
# e.g. ["coax_3", "optical_1"]. A zone already routed to a hidden input still
# shows that input's name; it's just no longer offered for selection.
CONF_DISABLED_SOURCES: Final = "disabled_sources"

# Input types and the ProAudio zones. Zones (outputs) drive entity creation,
# so they must be right; per-type input counts drive the source selector.
KIND_ANALOG: Final = "analog"
KIND_COAX: Final = "coax"
KIND_OPTICAL: Final = "optical"
KIND_ORDER: Final = (KIND_ANALOG, KIND_COAX, KIND_OPTICAL)
KIND_LABEL: Final[dict[str, str]] = {
    KIND_ANALOG: "RCA",
    KIND_COAX: "Coax",
    KIND_OPTICAL: "Optical",
}

# Extended I/O source-number base per input kind (XS bit 15 / value 32768 set).
# In Extended I/O mode every ProAudio model shares the same source map:
#   0     = disconnect (mute)
#   1-32  = analog RCA        (base 0)
#   33-64 = coax PCM/Dolby/DTS (base 32)
#   65-80 = optical           (base 64, only 16 slots)
XS_EXTENDED_IO_FLAG: Final = 32768
# Other XS control bits we normalise at setup for deterministic replies:
XS_ASY_FLAG: Final = 1  # async unsolicited responses (we turn OFF)
XS_ACK_FLAG: Final = 2  # "^+$" acknowledgements (we turn ON)
XS_ECO_FLAG: Final = 4  # echo "^=...$" on set commands (we turn ON)
# 'settings2' bit: UVL (value 4). When set, a direct 'VPZ'/'VZ' unmutes the
# zone. We clear it so the volume slider can stage a level on a muted/off zone
# without unmuting, letting source-select fade in to that level.
XS_UVL_FLAG: Final = 4
SOURCE_DISCONNECT: Final = 0
SOURCE_BASE: Final[dict[str, int]] = {
    KIND_ANALOG: 0,
    KIND_COAX: 32,
    KIND_OPTICAL: 64,
}
# Hard limits of each Extended I/O range (optical only spans 65-80).
KIND_MAX: Final[dict[str, int]] = {
    KIND_ANALOG: 32,
    KIND_COAX: 32,
    KIND_OPTICAL: 16,
}

# Per-model input-type counts and zone (output) count. All confirmed against
# pulse-eight.com spec sheets (analog / coax / optical inputs -> zones):
#   ProAudio 8     ->  8 /  8 /  8 ->  8 zones
#   ProAudio 16    -> 16 / 16 / 16 -> 16 zones
#   ProAudio 16 RS -> 16 / 16 /  8 -> 16 zones (+8 RJ45 remote sources,
#                     undocumented in the V2.1 manual, so not exposed)
#   ProAudio 1632  -> 16 / 16 /  8 -> 32 zones
#   ProAudio 32    -> 32 / 32 / 16 -> 32 zones
#   ProAudio 3248  -> 32 / 32 / 16 -> 48 zones
#   ProAudio 3264  -> 32 / 32 / 16 -> 64 zones
MODELS: Final[dict[str, dict[str, int]]] = {
    "ProAudio 8": {"zones": 8, "analog": 8, "coax": 8, "optical": 8},
    "ProAudio 16": {"zones": 16, "analog": 16, "coax": 16, "optical": 16},
    "ProAudio 16 RS": {"zones": 16, "analog": 16, "coax": 16, "optical": 8},
    "ProAudio 1632": {"zones": 32, "analog": 16, "coax": 16, "optical": 8},
    "ProAudio 32": {"zones": 32, "analog": 32, "coax": 32, "optical": 16},
    "ProAudio 3248": {"zones": 48, "analog": 32, "coax": 32, "optical": 16},
    "ProAudio 3264": {"zones": 64, "analog": 32, "coax": 32, "optical": 16},
}

DEFAULT_MODEL: Final = "ProAudio 16"
DEFAULT_EXTENDED_IO: Final = True

# --- Volume ----------------------------------------------------------------
# 'VPZ' sets/reads volume as a 0-100 percentage; 0 is full mute.
VOLUME_MIN: Final = 0
VOLUME_MAX: Final = 100

# --- Disconnect / fade-in --------------------------------------------------
# Source-list entry that disconnects a zone (also the label shown while Off).
SOURCE_OFF_LABEL: Final = "Off"
# Fade-in time when selecting a source, via the 'VMZ' mute fade ('VMLZ'/'VMT').
FADE_SECONDS: Final = 3
# 'VMT' takes two values: a time (timed mode, 100 ms steps: 10 = 1 s, 1-100)
# and a slope (sloped mode, 1-255). We drive timed mode via VMLZ, so the time
# sets the fade duration and the slope stays at its default.
VMT_FADE_STEPS: Final = FADE_SECONDS * 10
VMT_SLOPE_DEFAULT: Final = 160
# 'VMLZ' full-depth mute in timed mode (10000 + 248) so unmute fades all the way.
VMLZ_TIMED_FULL_MUTE: Final = 10248

# --- Coordinator -----------------------------------------------------------
SCAN_INTERVAL_SECONDS: Final = 30

MANUFACTURER: Final = "Pulse-Eight"
MODEL: Final = "ProAudio"  # refined from the '^V ?$' response at runtime
