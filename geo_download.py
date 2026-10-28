#!/usr/bin/env python3
"""Download the IP blocks of a country and store them as aggregated subnets.

The downloaded ranges are parsed, normalized and collapsed so that the
resulting file contains the smallest possible set of CIDR subnets covering
exactly the same address space.

Usage:
    python geo_download.py tr
    python geo_download.py de --ipv6
    python geo_download.py us -o /etc/firewall/us.txt --quiet

Copyright (C) 2026 Cuma Kurt <cumakurt@gmail.com>

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import TextIO

USER_AGENT = "geo-download/1.1 (+https://github.com/ipverse/rir-ip)"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
READ_CHUNK_SIZE = 64 * 1024

# (source name, address family) -> URL template. The placeholder is the
# lowercase ISO 3166-1 alpha-2 country code.
SOURCES = {
    ("ipverse", 4): "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/{cc}/ipv4-aggregated.txt",
    ("ipverse", 6): "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/{cc}/ipv6-aggregated.txt",
    ("ipdeny", 4): "https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone",
    ("ipdeny", 6): "https://www.ipdeny.com/ipv6/ipaddresses/aggregated/{cc}-aggregated.zone",
}

# Per source, in the order they are tried when the source is auto-selected.
AUTO_SOURCE_ORDER = ("ipverse", "ipdeny")

COUNTRY_CODE_PATTERN = re.compile(r"^[A-Za-z]{2}$")
PERMANENT_HTTP_STATUS = frozenset({400, 401, 403, 404})

# ISO 3166-1 alpha-2 codes. Validated locally because ipdeny.com answers
# unknown country codes with HTTP 200 and the full IPv4 table, which would
# silently produce a wrong result.
ISO_COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()
)

# Console rendering settings.
ANSI_RESET = "\x1b[0m"
ANSI_STYLES = {
    "accent": "\x1b[38;5;45m",
    "ok": "\x1b[38;5;41m",
    "warn": "\x1b[38;5;214m",
    "error": "\x1b[38;5;203m",
    "muted": "\x1b[38;5;244m",
    "bold": "\x1b[1m",
}
PROGRESS_REFRESH_SECONDS = 0.08
PROGRESS_LABEL_WIDTH = 22
PROGRESS_BAR_MIN_WIDTH = 8
PROGRESS_BAR_MAX_WIDTH = 36
BYTE_UNITS = ("B", "KB", "MB", "GB")


class DownloadError(RuntimeError):
    """Raised when no source could provide data for the requested country."""


class Console:
    """Small terminal UI with color, unicode and TTY detection."""

    def __init__(
        self,
        stream: TextIO | None = None,
        quiet: bool = False,
        show_progress: bool = True,
    ) -> None:
        self.stream = stream or sys.stdout
        self.quiet = quiet
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.uses_color = (
            is_tty
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM", "") != "dumb"
        )
        self.uses_unicode = is_tty and self._can_encode("━─⠋✔·→")
        self.progress_enabled = is_tty and not quiet and show_progress

        self.bar_full = "━" if self.uses_unicode else "#"
        self.bar_empty = "─" if self.uses_unicode else "-"
        self.spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if self.uses_unicode else "|/-\\"
        self.mark = "✔" if self.uses_unicode else "+"
        self.bullet = "·" if self.uses_unicode else "-"
        self.arrow = "→" if self.uses_unicode else "->"

    def _can_encode(self, text: str) -> bool:
        encoding = getattr(self.stream, "encoding", None) or "ascii"
        try:
            text.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    def style(self, text: str, name: str) -> str:
        if not self.uses_color:
            return text
        return f"{ANSI_STYLES[name]}{text}{ANSI_RESET}"

    def line(self, text: str = "") -> None:
        if self.quiet:
            return
        self.stream.write(text + "\n")
        self.stream.flush()

    def title(self, text: str) -> None:
        self.line()
        self.line("  " + self.style(text, "bold"))
        self.line()

    def success(self, text: str) -> None:
        self.line(f"  {self.style(self.mark, 'ok')} {text}")

    def warn(self, text: str) -> None:
        self.line(f"  {self.style('!', 'warn')} {self.style(text, 'muted')}")

    def draw_progress(self, text: str) -> None:
        self.stream.write("\r" + text + "\x1b[K")
        self.stream.flush()

    def clear_progress(self) -> None:
        self.stream.write("\r\x1b[K")
        self.stream.flush()


class DownloadProgress:
    """Renders a single-line progress bar for one HTTP download."""

    def __init__(self, console: Console, label: str) -> None:
        self.console = console
        self.label = label
        self.total: int | None = None
        self.downloaded = 0
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self.last_render = 0.0
        self.visible = console.progress_enabled

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(end - self.started, 0.0)

    def start(self, total: int | None) -> None:
        self.total = total
        self.started = time.monotonic()
        self._render(force=True)

    def update(self, downloaded: int) -> None:
        self.downloaded = downloaded
        self._render()

    def stop(self) -> None:
        """Freeze the counters and remove the progress line from the screen."""
        if self.finished_at is None:
            self.finished_at = time.monotonic()
        if self.visible:
            self.console.clear_progress()
            self.visible = False

    def _render(self, force: bool = False) -> None:
        if not self.visible:
            return
        now = time.monotonic()
        if not force and now - self.last_render < PROGRESS_REFRESH_SECONDS:
            return
        self.last_render = now
        self.console.draw_progress(self._compose(now))

    def _compose(self, now: float) -> str:
        console = self.console
        elapsed = max(now - self.started, 0.001)
        speed = self.downloaded / elapsed

        label = self.label
        if len(label) < PROGRESS_LABEL_WIDTH:
            label = label.ljust(PROGRESS_LABEL_WIDTH)

        if self.total:
            fraction = min(self.downloaded / self.total, 1.0)
            leading = console.style(f"{fraction * 100:3.0f}%", "bold")
            details = [
                f"{format_bytes(self.downloaded)} / {format_bytes(self.total)}",
                f"{format_bytes(speed)}/s",
            ]
            remaining = (self.total - self.downloaded) / speed if speed > 0 else 0.0
            if remaining >= 1.0:
                details.append(f"eta {format_eta(remaining)}")
            bar_fraction: float | None = fraction
        else:
            spinner = console.spinner[int(elapsed * 10) % len(console.spinner)]
            leading = console.style(spinner, "accent")
            details = [format_bytes(self.downloaded), f"{format_bytes(speed)}/s"]
            bar_fraction = None

        plain_details = "  ".join(details)
        columns = shutil.get_terminal_size((80, 24)).columns
        reserved = len(label) + len(plain_details) + 8
        bar_width = max(PROGRESS_BAR_MIN_WIDTH, min(PROGRESS_BAR_MAX_WIDTH, columns - reserved))

        if bar_fraction is None:
            bar = console.style(console.bar_empty * bar_width, "muted")
        else:
            filled = int(round(bar_fraction * bar_width))
            bar = console.style(console.bar_full * filled, "accent") + console.style(
                console.bar_empty * (bar_width - filled), "muted"
            )

        return f"  {label} {leading} {bar}  {console.style(plain_details, 'muted')}"


def format_bytes(value: float) -> str:
    """Format a byte count with binary units, for example ``28.1 KB``."""
    amount = float(value)
    for unit in BYTE_UNITS:
        if amount < 1024 or unit == BYTE_UNITS[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {BYTE_UNITS[-1]}"


def format_eta(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a country's IP blocks, aggregate them into CIDR subnets\n"
            "and save them as <country>.txt in the current directory."
        ),
        epilog=(
            "Examples:\n"
            "  geo_download.py tr\n"
            "  geo_download.py de --ipv6\n"
            "  geo_download.py us -o /etc/firewall/us.txt --no-progress\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "country",
        help="ISO 3166-1 alpha-2 country code, for example 'tr'.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: <country>.txt in the current directory).",
    )
    parser.add_argument(
        "--ipv6",
        action="store_true",
        help="Also download IPv6 blocks and write them to the same file.",
    )
    parser.add_argument(
        "--source",
        choices=("auto", *AUTO_SOURCE_ORDER),
        default="auto",
        help="Data source to use (default: auto, tries sources in order).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Network timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress bar (it is also disabled when stdout is not a TTY).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all informational output on stdout.",
    )
    args = parser.parse_args(argv)

    if not COUNTRY_CODE_PATTERN.match(args.country):
        parser.error(f"invalid country code: {args.country!r} (expected two letters, e.g. 'tr')")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    args.country = args.country.upper()
    if args.country not in ISO_COUNTRY_CODES:
        parser.error(f"unknown country code: {args.country!r} (expected an ISO 3166-1 alpha-2 code)")
    return args


def content_length(response) -> int | None:
    """Return a usable Content-Length header value, if the server sent one."""
    header = response.headers.get("Content-Length")
    if not header:
        return None
    try:
        value = int(header)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def read_body(
    response,
    limit: int = MAX_DOWNLOAD_BYTES,
    progress: DownloadProgress | None = None,
) -> bytes:
    """Read the whole response body, aborting if it exceeds ``limit`` bytes."""
    expected = content_length(response)
    if expected is not None and expected > limit:
        raise DownloadError(f"response larger than {limit} bytes")

    if progress is not None:
        progress.start(expected)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise DownloadError(f"response larger than {limit} bytes")
        chunks.append(chunk)
        if progress is not None:
            progress.update(total)

    if progress is not None:
        progress.stop()
    return b"".join(chunks)


def fetch_text(url: str, timeout: float, progress: DownloadProgress | None = None) -> str:
    """Download ``url`` as text, retrying transient failures with backoff."""
    last_error: Exception | None = None

    for attempt in range(RETRY_ATTEMPTS):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain, */*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return read_body(response, progress=progress).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in PERMANENT_HTTP_STATUS:
                raise DownloadError(f"{url} returned HTTP {error.code}") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
        except DownloadError:
            raise
        finally:
            if progress is not None:
                progress.stop()

        if attempt + 1 < RETRY_ATTEMPTS:
            delay = RETRY_BASE_DELAY_SECONDS * (2**attempt)
            time.sleep(delay + random.uniform(0, delay / 2))

    raise DownloadError(f"failed to download {url}: {last_error}") from last_error


def parse_networks(text: str, family: int) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse CIDR lines from a zone file, ignoring comments and blank lines."""
    networks = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            # Providers occasionally emit ranges or hostnames; skip them
            # instead of failing the whole download.
            continue
        if network.version == family:
            networks.append(network)
    return networks


def collapse(networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> list:
    """Merge adjacent and overlapping networks into the smallest set."""
    collapsed = list(ipaddress.collapse_addresses(networks))
    collapsed.sort(key=lambda network: (network.version, int(network.network_address), network.prefixlen))
    return collapsed


def download_country(
    country: str,
    family: int,
    source: str,
    timeout: float,
    console: Console,
) -> tuple[list[ipaddress.IPv4Network | ipaddress.IPv6Network], str, DownloadProgress]:
    """Return networks for one address family from the first usable source."""
    sources = AUTO_SOURCE_ORDER if source == "auto" else (source,)
    errors: list[str] = []

    for index, candidate in enumerate(sources):
        url = SOURCES[(candidate, family)].format(cc=country.lower())
        progress = DownloadProgress(console, f"IPv{family} {console.bullet} {candidate}")
        try:
            text = fetch_text(url, timeout, progress)
        except DownloadError as error:
            progress.stop()
            errors.append(f"{candidate}: {error}")
            if index + 1 < len(sources):
                console.warn(f"{candidate} unavailable; falling back to {sources[index + 1]}")
            continue

        networks = parse_networks(text, family)
        if not networks:
            errors.append(f"{candidate}: no valid IPv{family} networks returned by {url}")
            if index + 1 < len(sources):
                console.warn(f"{candidate} returned no IPv{family} networks; trying {sources[index + 1]}")
            continue

        return networks, candidate, progress

    detail = "; ".join(errors) if errors else "no sources configured"
    raise DownloadError(f"could not fetch IPv{family} blocks for {country}: {detail}")


def write_atomically(path: str, lines: list[str]) -> None:
    """Write ``lines`` to ``path`` via a temporary file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=directory,
        prefix=".geo_download-",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = handle.name
    try:
        with handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def report_error(message: str) -> None:
    text = f"error: {message}"
    stream = sys.stderr
    if getattr(stream, "isatty", lambda: False)() and "NO_COLOR" not in os.environ:
        text = f"{ANSI_STYLES['error']}{text}{ANSI_RESET}"
    print(text, file=stream)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console(quiet=args.quiet, show_progress=not args.no_progress)
    families = (4, 6) if args.ipv6 else (4,)
    output_path = args.output or f"{args.country.lower()}.txt"
    started = time.monotonic()

    console.title(f"geo-download {console.bullet} {args.country}")

    all_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        for family in families:
            networks, used_source, progress = download_country(
                args.country, family, args.source, args.timeout, console
            )
            networks = collapse(networks)
            all_networks.extend(networks)

            address_count = sum(network.num_addresses for network in networks)
            console.success(
                f"IPv{family} {console.bullet} {used_source} {console.bullet} "
                f"{len(networks):,} subnets {console.bullet} {address_count:,} addresses "
                f"{console.bullet} {format_bytes(progress.downloaded)} in "
                f"{format_duration(progress.elapsed)}"
            )
    except DownloadError as error:
        report_error(str(error))
        return 1
    except KeyboardInterrupt:
        console.clear_progress()
        report_error("interrupted")
        return 130

    if not all_networks:
        report_error("no networks to write")
        return 1

    try:
        write_atomically(output_path, [str(network) for network in all_networks])
    except OSError as error:
        report_error(f"cannot write {output_path}: {error}")
        return 1

    console.line()
    console.line(
        f"  {console.style(console.arrow, 'accent')} "
        f"{console.style(os.path.abspath(output_path), 'bold')} "
        f"{console.bullet} {len(all_networks):,} subnets "
        f"{console.bullet} {format_duration(time.monotonic() - started)}"
    )
    console.line()
    return 0


if __name__ == "__main__":
    sys.exit(main())
