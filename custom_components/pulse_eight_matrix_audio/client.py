"""Async TCP/IP client for the Pulse-Eight ProAudio Universal Serial Protocol.

Implements the ASCII command framing described in the Universal Serial Protocol
Guide V2.1:

  * Every command is ``^CMD params$``.
  * Replies are ``^+$`` (ack), ``^!<n>$`` (error), or ``^=CMD params$`` (query
    / echo). The switch may also append CR/LF outside the ``^...$`` frame and
    may emit unsolicited ``^=...$`` frames (ASY mode) which we skip.
  * Query response params are fixed width, zero padded (zone/source = 3 digits).

The socket is a raw TCP connection to port 50005 that the switch keeps open
until closed or 10 minutes idle. We hold one persistent connection guarded by a
lock and reconnect transparently on failure.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from .const import DEFAULT_TIMEOUT, XS_EXTENDED_IO_FLAG

_LOGGER = logging.getLogger(__name__)

# Error-code table from the protocol guide ("The Error Response").
_ERROR_TEXT: dict[int, str] = {
    1: "Unrecognized command",
    2: "Parameter out of range",
    3: "Syntax error / badly formed command",
    5: "Wrong number of parameters",
    6: "Device busy",
    7: "Buffer overflow",
    8: "Command not valid while device is powered off",
}

# One ^...$ frame. CR/LF and stray bytes between frames are ignored.
_FRAME_RE = re.compile(rb"\^([^$]*)\$")


class PulseEightError(Exception):
    """Base error for the Pulse-Eight client."""


class PulseEightConnectionError(PulseEightError):
    """Raised when the matrix cannot be reached or the link drops."""


class PulseEightCommandError(PulseEightError):
    """Raised when the switch returns a ``^!<n>$`` error response."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Error {code}: {_ERROR_TEXT.get(code, 'unknown error')}")


@dataclass
class DeviceInfo:
    """Parsed ``^=V ...$`` version response."""

    model: str
    firmware: str
    serial: str


@dataclass
class MatrixState:
    """Snapshot of the matrix state polled from the device."""

    # output/zone index (1-based) -> routed source index (1-based, 0 = none)
    routes: dict[int, int] = field(default_factory=dict)
    # output/zone index (1-based) -> muted?
    mutes: dict[int, bool] = field(default_factory=dict)
    # output/zone index (1-based) -> volume 0..100
    volumes: dict[int, int] = field(default_factory=dict)


class PulseEightClient:
    """Persistent async TCP client for a single ProAudio matrix."""

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = bytearray()

    # --- Connection management --------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
            self._buffer.clear()
        except (OSError, asyncio.TimeoutError) as err:
            raise PulseEightConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {err}"
            ) from err

    async def async_close(self) -> None:
        """Close the connection (call on unload)."""
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    # --- Low-level command exchange ---------------------------------------

    async def _command(self, verb: str, body: str = "", expected: int = 1) -> list[str]:
        """Send ``^<verb> <body>$`` and return the params of matching ``^=`` frames.

        With ACK+ECO on (the factory default) the switch replies ``^+$`` then one
        ``^=<verb> ...$`` echo per affected zone. ``expected`` is how many echo
        frames to wait for (1 for a set/version, ``outputs`` for a batch query);
        we return as soon as the ack and that many frames arrive so a command
        costs ~one round trip, not a full timeout window. A ``^!<n>$`` frame
        raises :class:`PulseEightCommandError`; other async frames are skipped.
        """
        async with self._lock:
            try:
                return await self._command_locked(verb, body, expected)
            except (OSError, asyncio.TimeoutError) as err:
                await self._disconnect()
                raise PulseEightConnectionError(
                    f"Communication error with {self._host}: {err}"
                ) from err

    async def _command_locked(
        self, verb: str, body: str, expected: int
    ) -> list[str]:
        await self._ensure_connected()
        assert self._writer is not None and self._reader is not None

        command = f"^{verb} {body}$" if body else f"^{verb}$"
        self._writer.write(command.encode("ascii"))
        await self._writer.drain()

        prefix = f"={verb} "
        results: list[str] = []
        acked = False
        while not (acked and len(results) >= expected):
            try:
                frame = await asyncio.wait_for(
                    self._read_frame(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                # A zone may not answer (e.g. it doesn't exist); return whatever
                # arrived rather than hanging, but surface a total silence.
                if acked or results:
                    break
                raise
            if frame == "+":
                acked = True
            elif frame.startswith("!"):
                raise PulseEightCommandError(int(frame[1:] or 0))
            elif frame.startswith(prefix):
                results.append(frame[len(prefix):])
            # else: unrelated async/echo frame for another command; ignore.
        return results

    async def _read_frame(self) -> str:
        """Read and return the text inside the next ``^...$`` frame."""
        assert self._reader is not None
        while True:
            match = _FRAME_RE.search(self._buffer)
            if match:
                # Copy the captured bytes out BEFORE mutating the buffer: the
                # match re-slices its (mutable) bytearray lazily, so deleting
                # first would corrupt group(1).
                frame = bytes(match.group(1))
                del self._buffer[: match.end()]
                return frame.decode("ascii", errors="replace").strip()
            chunk = await self._reader.read(256)
            if not chunk:
                raise PulseEightConnectionError("Connection closed by switch")
            self._buffer.extend(chunk)

    # --- High-level API ----------------------------------------------------

    async def async_get_version(self) -> DeviceInfo:
        """Query ``^V ?$`` -> model, firmware, serial. Also used as a probe."""
        results = await self._command("V", "?")
        if not results:
            raise PulseEightConnectionError("No version response from switch")
        # e.g.  "ProAudio16",1.23a,59B2S12345678
        parts = results[0].split(",")
        model = parts[0].strip().strip('"') if parts else "ProAudio"
        firmware = parts[1].strip() if len(parts) > 1 else ""
        serial = parts[2].strip() if len(parts) > 2 else ""
        return DeviceInfo(model=model, firmware=firmware, serial=serial)

    async def async_test_connection(self) -> DeviceInfo:
        """Validate connectivity during config flow."""
        return await self.async_get_version()

    async def async_enable_extended_io(self) -> None:
        """Set the XIO flag so source numbering is model-independent."""
        # '+' prefix sets just this bit without disturbing the others.
        await self._command("XS", f"+{XS_EXTENDED_IO_FLAG}")

    async def async_get_state(self, outputs: int) -> MatrixState:
        """Poll routing, mute and volume for zones 1..outputs."""
        state = MatrixState()
        if outputs < 1:
            return state
        zones = "".join(f"@{z}" for z in range(1, outputs + 1))

        for raw in await self._command("SZ", f"{zones},?", expected=outputs):
            zone, source = _parse_pair(raw)
            if zone is not None:
                state.routes[zone] = source
        for raw in await self._command("VMZ", f"{zones},?", expected=outputs):
            zone, mute = _parse_pair(raw)
            if zone is not None:
                state.mutes[zone] = bool(mute)
        for raw in await self._command("VPZ", f"{zones},?", expected=outputs):
            zone, vol = _parse_pair(raw)
            if zone is not None:
                state.volumes[zone] = vol
        return state

    async def async_set_route(self, output: int, source: int) -> None:
        """Route ``source`` to ``output`` (analog audio switch, 'SZ')."""
        await self._command("SZ", f"@{output},{source}")

    async def async_set_mute(self, output: int, muted: bool) -> None:
        """Audio-mute or unmute an output ('VMZ')."""
        await self._command("VMZ", f"@{output},{1 if muted else 0}")

    async def async_set_volume(self, output: int, volume: int) -> None:
        """Set output volume as a 0-100 percentage ('VPZ')."""
        await self._command("VPZ", f"@{output},{volume}")


def _parse_pair(raw: str) -> tuple[int | None, int]:
    """Parse a ``@001,002`` style response body into (zone, value)."""
    body = raw.strip()
    if body.startswith("@"):
        body = body[1:]
    parts = body.split(",")
    if len(parts) < 2:
        return None, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, 0
