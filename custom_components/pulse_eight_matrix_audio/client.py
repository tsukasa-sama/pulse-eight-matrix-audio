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

from .const import (
    DEFAULT_TIMEOUT,
    XS_ACK_FLAG,
    XS_ASY_FLAG,
    XS_ECO_FLAG,
    XS_EXTENDED_IO_FLAG,
)

_LOGGER = logging.getLogger(__name__)

# Once the first reply byte arrives, remaining frames of a multi-frame response
# come fast (the protocol guide promises <100ms). If nothing more arrives within
# this gap, the response is complete. Kept independent of the connect timeout so
# ACK-disabled units don't stall a full timeout per command.
_INTER_FRAME_TIMEOUT: float = 0.5

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
    """Connect-per-command async TCP client for a single ProAudio matrix.

    The switch keeps a connection open until the client closes it or 10 minutes
    elapse, and services only a small number of sockets. A persistent connection
    would leak a socket on any HA restart/crash and block reconnection for up to
    10 minutes, so instead we open a fresh connection per command and close it
    immediately. Commands are serialised by a lock.
    """

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._lock = asyncio.Lock()
        # The socket of the command currently in flight, if any. Commands are
        # serialised by the lock, so at most one exists at a time. Tracked so a
        # teardown (async_close) can force it shut instead of leaving a half-open
        # socket the switch will hold for up to 10 minutes.
        self._writer: asyncio.StreamWriter | None = None

    async def async_close(self) -> None:
        """Abort any in-flight command's socket so the switch frees it now.

        Per-command connections normally close themselves. But if a command is
        still running when the entry is torn down (e.g. a poll in flight during a
        HACS reload), its socket would linger on the switch — which services only
        a few sockets and holds them ~10 minutes — and starve the reconnect. An
        immediate abort (RST) tells the switch the socket is gone right away.
        """
        writer = self._writer
        if writer is not None:
            self._writer = None
            _abort_writer(writer)

    # --- Low-level command exchange ---------------------------------------

    async def _command(
        self,
        verb: str,
        body: str = "",
        expected: int = 1,
        require_response: bool = True,
    ) -> list[str]:
        """Send ``^<verb> <body>$`` and return the params of matching ``^=`` frames.

        Opens a connection, runs the exchange, and closes it. Response handling
        does NOT depend on the ``^+$`` ack (units can have ACK disabled): we read
        the first frame within the connect timeout, then keep reading with a
        short inter-frame gap, returning once ``expected`` data frames arrive or
        the gap elapses. ``expected`` is how many ``^=<verb>`` frames to wait for
        (1 for a set/version, ``outputs`` for a batch query).

        A ``^!<n>$`` frame raises :class:`PulseEightCommandError`; other frames
        (acks, async/echo frames for other commands) are skipped. With
        ``require_response=False`` the command is fire-and-forget: any replies
        are drained but total silence is not an error (used for XS config, which
        may not reply until ACK/ECO are enabled).
        """
        sent = f"^{verb} {body}$" if body else f"^{verb}$"
        async with self._lock:
            _LOGGER.debug("Connecting to %s:%s", self._host, self._port)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=self._timeout,
                )
            except (OSError, asyncio.TimeoutError) as err:
                raise PulseEightConnectionError(
                    f"Cannot connect to {self._host}:{self._port}: {err!r}"
                ) from err
            self._writer = writer
            try:
                return await self._exchange(
                    reader, writer, verb, body, expected, require_response
                )
            except asyncio.TimeoutError as err:
                raise PulseEightConnectionError(
                    f"No response from {self._host}:{self._port} to {sent!r} "
                    f"within {self._timeout}s (connected, but the switch sent "
                    f"nothing back)"
                ) from err
            except OSError as err:
                raise PulseEightConnectionError(
                    f"Communication error with {self._host}:{self._port} on "
                    f"{sent!r}: {err!r}"
                ) from err
            finally:
                self._writer = None
                writer.close()
                # wait_closed() confirms the graceful FIN, but re-raises if we're
                # here because the task was cancelled (reload/shutdown). Shield it
                # so the close still completes; the close itself is already
                # scheduled by writer.close() regardless.
                try:
                    await asyncio.shield(writer.wait_closed())
                except (OSError, asyncio.CancelledError):
                    pass

    async def _exchange(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        verb: str,
        body: str,
        expected: int,
        require_response: bool,
    ) -> list[str]:
        command = f"^{verb} {body}$" if body else f"^{verb}$"
        _LOGGER.debug("TX %s", command)
        writer.write(command.encode("ascii"))
        await writer.drain()

        prefix = f"={verb} "
        buffer = bytearray()
        results: list[str] = []
        first = True
        while expected <= 0 or len(results) < expected:
            # Full timeout only while awaiting the first frame of a command we
            # require a reply to (detects a dead link); a short gap otherwise, so
            # an ACK-off unit that sends only its data frame — or a fire-and-
            # forget command that sends nothing — returns promptly.
            timeout = (
                self._timeout
                if first and require_response
                else _INTER_FRAME_TIMEOUT
            )
            try:
                frame = await asyncio.wait_for(
                    self._read_frame(reader, buffer), timeout=timeout
                )
            except asyncio.TimeoutError:
                if first and require_response:
                    _LOGGER.debug("RX timeout (no response) for %s", command)
                    raise
                # Fire-and-forget, or no more frames coming: we're done.
                break
            first = False
            _LOGGER.debug("RX ^%s$", frame)
            self._collect_frame(frame, prefix, results)
        return results

    @staticmethod
    def _collect_frame(frame: str, prefix: str, results: list[str]) -> None:
        """Classify one frame: append data, raise on error, skip the rest."""
        if frame.startswith("!"):
            raise PulseEightCommandError(int(frame[1:] or 0))
        if frame.startswith(prefix):
            results.append(frame[len(prefix):])
        # "+" acks and async/echo frames for other commands are ignored.

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader, buffer: bytearray) -> str:
        """Read and return the text inside the next ``^...$`` frame."""
        while True:
            match = _FRAME_RE.search(buffer)
            if match:
                # Copy the captured bytes out BEFORE mutating the buffer: the
                # match re-slices its (mutable) bytearray lazily, so deleting
                # first would corrupt group(1).
                frame = bytes(match.group(1))
                del buffer[: match.end()]
                return frame.decode("ascii", errors="replace").strip()
            chunk = await reader.read(256)
            if not chunk:
                raise PulseEightConnectionError("Connection closed by switch")
            buffer.extend(chunk)

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
        info = DeviceInfo(model=model, firmware=firmware, serial=serial)
        _LOGGER.debug("Parsed version: %s", info)
        return info

    async def async_test_connection(self) -> DeviceInfo:
        """Validate connectivity during config flow."""
        return await self.async_get_version()

    async def async_configure(self, extended_io: bool = True) -> None:
        """Normalise the XS control flags for predictable behaviour.

        Turns ACK and ECO on (so replies are deterministic) and ASY off (no
        unsolicited frames to desync the reader), and optionally sets XIO for
        model-independent source numbering. Fire-and-forget: these run before we
        know the current ACK state, so a missing reply is not an error.
        """
        set_bits = XS_ACK_FLAG | XS_ECO_FLAG
        if extended_io:
            set_bits |= XS_EXTENDED_IO_FLAG
        _LOGGER.debug(
            "Configuring XS: set +%d (ACK/ECO%s), clear -%d (ASY)",
            set_bits, "/XIO" if extended_io else "", XS_ASY_FLAG,
        )
        # '+' sets the listed bits, '-' clears them, without touching the rest.
        await self._command("XS", f"+{set_bits}", require_response=False)
        await self._command("XS", f"-{XS_ASY_FLAG}", require_response=False)

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
        _LOGGER.debug("Set route: zone %d -> source %d", output, source)
        await self._command("SZ", f"@{output},{source}")

    async def async_set_mute(self, output: int, muted: bool) -> None:
        """Audio-mute or unmute an output ('VMZ')."""
        _LOGGER.debug("Set mute: zone %d -> %s", output, muted)
        await self._command("VMZ", f"@{output},{1 if muted else 0}")

    async def async_set_volume(self, output: int, volume: int) -> None:
        """Set output volume as a 0-100 percentage ('VPZ')."""
        _LOGGER.debug("Set volume: zone %d -> %d%%", output, volume)
        await self._command("VPZ", f"@{output},{volume}")


def _abort_writer(writer: asyncio.StreamWriter) -> None:
    """Force a socket shut immediately (RST), tolerating a dead transport.

    Used on teardown: ``transport.abort()`` skips the graceful FIN flush so the
    switch sees the socket drop at once. Falls back to ``close()`` if abort is
    unavailable, and swallows errors from an already-broken transport.
    """
    try:
        transport = writer.transport
        if transport is not None and hasattr(transport, "abort"):
            transport.abort()
        else:
            writer.close()
    except Exception:
        # Best-effort teardown: an already-broken transport must never propagate.
        pass


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
