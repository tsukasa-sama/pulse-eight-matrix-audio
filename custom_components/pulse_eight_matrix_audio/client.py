"""Async TCP/IP client for the Pulse-Eight ProAudio Universal Serial Protocol.

Implements the ASCII command framing described in the Universal Serial Protocol
Guide V2.1:

  * Every command is ``^CMD params$``.
  * Replies are ``^+$`` (ack), ``^!<n>$`` (error), or ``^=CMD params$`` (query
    / echo). The switch may also append CR/LF outside the ``^...$`` frame and
    may emit unsolicited ``^=...$`` frames (ASY mode) which we skip.
  * Query response params are fixed width, zero padded (zone/source = 3 digits).

The socket is a raw TCP connection to port 50005 that the switch keeps open
until closed or 10 minutes idle. The switch services only a few sockets, so we
open a fresh connection per exchange, serialise them with a lock, and force it
shut with a TCP RST (abort) the moment we're done — a graceful FIN leaves the
switch's command-oriented socket in CLOSE_WAIT, holding a pool slot until its
10-minute idle timer and eventually starving new connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field

from .const import (
    DEFAULT_TIMEOUT,
    VMLZ_TIMED_FULL_MUTE,
    VMT_FADE_STEPS,
    VMT_SLOPE_DEFAULT,
    XS_ACK_FLAG,
    XS_ASY_FLAG,
    XS_ECO_FLAG,
    XS_EXTENDED_IO_FLAG,
    XS_UVL_FLAG,
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

# A query/echo frame body: "=<verb>[.<chan>] <params>". The firmware appends a
# channel qualifier to per-channel commands (e.g. "SZ.2"); "V" carries none. A
# plain "=<verb> " prefix match would miss the qualified form and drop every
# poll response. Group 1 is the verb (sans qualifier), group 2 the params.
_RESP_RE = re.compile(r"=([A-Za-z]+)(?:\.\d+)?\s+(.*)")


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
    10 minutes, so instead we open a fresh connection per command and force it
    shut with a TCP RST as soon as we're done — a graceful FIN would leave the
    switch's socket in CLOSE_WAIT, holding a pool slot until its 10-minute idle
    timer. Commands are serialised by a lock.
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
        # Zones whose per-zone mute-fade (VMLZ/VMT) we've configured this
        # session; the switch retains the setting, so we send it once per zone.
        self._fade_configured: set[int] = set()

    async def async_close(self) -> None:
        """Abort any in-flight command's socket so the switch frees it now.

        Per-command connections RST-close themselves on completion. But if a
        command is still running when the entry is torn down (e.g. a poll in
        flight during a HACS reload), its socket would linger on the switch —
        which services only a few sockets and holds them ~10 minutes — and
        starve the reconnect. An immediate abort (RST) tells the switch the
        socket is gone right away.
        """
        writer = self._writer
        if writer is not None:
            self._writer = None
            _abort_writer(writer)

    # --- Low-level command exchange ---------------------------------------

    @contextlib.asynccontextmanager
    async def _open(self):
        """Open a serialised, per-command connection and always RST-close it.

        Holds the command lock, tracks the writer so a teardown can abort it,
        and on exit forces the socket shut with a TCP RST (``_abort_writer``)
        rather than a graceful FIN: the switch's command-oriented socket would
        otherwise sit in CLOSE_WAIT, holding one of its few pool slots until the
        10-minute idle timer and eventually refusing new connections.
        """
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
                yield reader, writer
            finally:
                self._writer = None
                _abort_writer(writer)

    async def _command(
        self,
        verb: str,
        body: str = "",
        expected: int = 1,
        require_response: bool = True,
    ) -> list[str]:
        """Send ``^<verb> <body>$`` and return the params of matching ``^=`` frames.

        Response handling does NOT depend on the ``^+$`` ack (units can have ACK
        disabled): we read the first frame within the connect timeout, then keep
        reading with a short inter-frame gap, returning once ``expected`` data
        frames arrive or the gap elapses. ``expected`` is how many ``^=<verb>``
        frames to wait for (1 for a set/version).

        A ``^!<n>$`` frame raises :class:`PulseEightCommandError`; acks and
        frames for other verbs are skipped. With ``require_response=False`` the
        command is fire-and-forget: any reply is drained but total silence is not
        an error (used for XS config, which may not reply until ACK/ECO are on).
        """
        sent = f"^{verb} {body}$" if body else f"^{verb}$"
        async with self._open() as (reader, writer):
            try:
                _LOGGER.debug("TX %s", sent)
                writer.write(sent.encode("ascii"))
                await writer.drain()
                frames = await self._read_frames(
                    reader, {verb}, expected, require_response
                )
                return frames[verb]
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

    async def _command_many(
        self, commands: list[tuple[str, str]], expected_per: int
    ) -> dict[str, list[str]]:
        """Run several queries over ONE connection; return params keyed by verb.

        The switch buffers commands until each ``$`` and executes them in order,
        returning all responses on the same socket, so batching the poll's
        queries into a single connection cuts socket churn against the switch's
        tiny pool. ``commands`` must use distinct verbs; ``expected_per`` is the
        number of ``^=`` frames expected from each.
        """
        payload = "".join(
            f"^{verb} {body}$" if body else f"^{verb}$" for verb, body in commands
        )
        verbs = {verb for verb, _ in commands}
        total = expected_per * len(commands)
        async with self._open() as (reader, writer):
            try:
                _LOGGER.debug("TX %s", payload)
                writer.write(payload.encode("ascii"))
                await writer.drain()
                return await self._read_frames(
                    reader, verbs, total, require_response=True
                )
            except asyncio.TimeoutError as err:
                raise PulseEightConnectionError(
                    f"No response from {self._host}:{self._port} to batch poll "
                    f"{payload!r} within {self._timeout}s"
                ) from err
            except OSError as err:
                raise PulseEightConnectionError(
                    f"Communication error with {self._host}:{self._port} on "
                    f"batch poll {payload!r}: {err!r}"
                ) from err

    async def _send(
        self, commands: list[tuple[str, str]], raise_on_error: bool = True
    ) -> None:
        """Fire a batch of set commands over ONE connection; drain any replies.

        The switch buffers commands until each ``$`` and executes them in the
        order given, so callers can sequence e.g. route -> unmute atomically on a
        single socket. With ``raise_on_error`` (default) a ``^!<n>$`` reply
        raises; pass False for best-effort config the switch may reject without
        failing the whole action. Acks and echo frames are drained.
        """
        payload = "".join(
            f"^{verb} {body}$" if body else f"^{verb}$" for verb, body in commands
        )
        verbs = {verb for verb, _ in commands}
        async with self._open() as (reader, writer):
            try:
                _LOGGER.debug("TX %s", payload)
                writer.write(payload.encode("ascii"))
                await writer.drain()
                # expected=0 + require_response=False: read/drain until the
                # inter-frame gap, then return.
                await self._read_frames(
                    reader, verbs, expected=0, require_response=False,
                    raise_on_error=raise_on_error,
                )
            except OSError as err:
                raise PulseEightConnectionError(
                    f"Communication error with {self._host}:{self._port} on "
                    f"{payload!r}: {err!r}"
                ) from err

    async def _read_frames(
        self,
        reader: asyncio.StreamReader,
        verbs: set[str],
        expected: int,
        require_response: bool,
        raise_on_error: bool = True,
    ) -> dict[str, list[str]]:
        """Read ``^...$`` frames, bucketing ``^=<verb>`` params by verb.

        Stops once ``expected`` data frames have been collected (across all
        ``verbs``) or the inter-frame gap elapses. The full connect timeout
        applies only while awaiting the first frame of a response we require, so
        an ACK-off unit — or a fire-and-forget command that sends nothing —
        returns promptly. A ``^!<n>$`` frame raises when ``raise_on_error``,
        else it is logged and skipped; ``^+$`` acks and frames for other verbs
        are skipped. ``_RESP_RE`` tolerates the firmware's channel qualifier
        (``SZ.2``), which a plain prefix match would miss.
        """
        results: dict[str, list[str]] = {verb: [] for verb in verbs}
        buffer = bytearray()
        collected = 0
        first = True
        while expected <= 0 or collected < expected:
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
                    _LOGGER.debug("RX timeout (no response)")
                    raise
                # Fire-and-forget, or no more frames coming: we're done.
                break
            first = False
            _LOGGER.debug("RX ^%s$", frame)
            if self._handle_frame(frame, verbs, results, raise_on_error):
                collected += 1
        return results

    @staticmethod
    def _handle_frame(
        frame: str,
        verbs: set[str],
        results: dict[str, list[str]],
        raise_on_error: bool,
    ) -> bool:
        """Route one frame into ``results``; return True if a data frame was kept.

        A ``^!<n>$`` frame raises when ``raise_on_error`` else is logged and
        dropped. ``^=<verb>$`` frames for a wanted verb are appended (channel
        qualifier ``SZ.2`` stripped by ``_RESP_RE``); ``+`` acks and frames for
        other verbs are ignored.
        """
        if frame.startswith("!"):
            code = int(frame[1:] or 0)
            if raise_on_error:
                raise PulseEightCommandError(code)
            _LOGGER.warning(
                "Switch rejected a command: error %d (%s)",
                code, _ERROR_TEXT.get(code, "unknown error"),
            )
            return False
        match = _RESP_RE.match(frame)
        if match and match.group(1) in verbs:
            results[match.group(1)].append(match.group(2))
            return True
        return False

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
        unsolicited frames to desync the reader), optionally sets XIO for
        model-independent source numbering, and clears UVL (settings2) so a
        direct volume set doesn't unmute — letting a muted/off zone stage its
        volume for the source-select fade-in. Fire-and-forget: these run before
        we know the current ACK state, so a missing reply is not an error.
        """
        set_bits = XS_ACK_FLAG | XS_ECO_FLAG
        if extended_io:
            set_bits |= XS_EXTENDED_IO_FLAG
        _LOGGER.debug(
            "Configuring XS: set +%d (ACK/ECO%s), clear -%d (ASY), clear UVL",
            set_bits, "/XIO" if extended_io else "", XS_ASY_FLAG,
        )
        # '+' sets the listed bits, '-' clears them, without touching the rest.
        await self._command("XS", f"+{set_bits}", require_response=False)
        await self._command("XS", f"-{XS_ASY_FLAG}", require_response=False)
        # A leading comma targets settings2 only; '-' clears the UVL bit there.
        await self._command("XS", f",-{XS_UVL_FLAG}", require_response=False)

    async def async_get_state(self, outputs: int) -> MatrixState:
        """Poll routing, mute and volume for zones 1..outputs in one exchange."""
        state = MatrixState()
        if outputs < 1:
            return state
        zones = "".join(f"@{z}" for z in range(1, outputs + 1))
        frames = await self._command_many(
            [("SZ", f"{zones},?"), ("VMZ", f"{zones},?"), ("VPZ", f"{zones},?")],
            expected_per=outputs,
        )
        for raw in frames.get("SZ", []):
            zone, source = _parse_pair(raw)
            if zone is not None:
                state.routes[zone] = source
        for raw in frames.get("VMZ", []):
            zone, mute = _parse_pair(raw)
            if zone is not None:
                state.mutes[zone] = bool(mute)
        for raw in frames.get("VPZ", []):
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

    async def async_disconnect(self, output: int) -> None:
        """Disconnect a zone: hard-cut the route and mute it, in one exchange.

        ``SZ @<zone>,0`` drops the source immediately (instant silence, and the
        zone reads as not-playing); ``VMZ @<zone>,1`` also mutes it so a later
        source-select can fade back in from the muted level.
        """
        _LOGGER.debug("Disconnect zone %d", output)
        await self._send(
            [("SZ", f"@{output},0"), ("VMZ", f"@{output},1")]
        )

    async def async_route_and_fade_in(self, output: int, source: int) -> None:
        """Route ``source`` to a zone and fade audio in to its set volume.

        The per-zone fade depth+time ('VMLZ'/'VMT') is configured once per
        session (best-effort — a unit that rejects it just uses its default fade
        timing). The essential step then routes the source and unmutes ('VMZ
        @zone,0'), fading up from the muted level. If the zone was already
        unmuted the unmute is a no-op and it simply switches source.
        """
        _LOGGER.debug("Route+fade zone %d -> source %d", output, source)
        if output not in self._fade_configured:
            self._fade_configured.add(output)
            await self._configure_fade(output)
        await self._send([("SZ", f"@{output},{source}"), ("VMZ", f"@{output},0")])

    async def _configure_fade(self, output: int) -> None:
        """Best-effort per-zone mute-fade setup for the source-select fade-in.

        Sends 'VMLZ' (full-depth timed mute) then 'VMT' (fade time) as separate
        best-effort commands, so a rejection is logged per-command (paired with
        its ``TX`` line) without failing source-select. The switch retains these
        settings, so this runs once per zone per session.
        """
        await self._send(
            [("VMLZ", f"@{output},{VMLZ_TIMED_FULL_MUTE}")], raise_on_error=False
        )
        # 'VMT' takes two values (time, slope); we set the fade time and leave
        # slope at its default. See the const notes on parameter order.
        await self._send(
            [("VMT", f"@{output},{VMT_FADE_STEPS},{VMT_SLOPE_DEFAULT}")],
            raise_on_error=False,
        )


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
