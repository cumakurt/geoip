#!/usr/bin/env python3
"""Download the IP blocks of a country and store them as aggregated subnets.

The downloaded ranges are parsed, normalized and collapsed so that the
resulting file contains the smallest possible set of CIDR subnets covering
exactly the same address space.

Usage:
    python geo_download.py tr
    python geo_download.py tr de us
    python geo_download.py de --family both --format nft
    python geo_download.py us --format nginx --stdout

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
import contextlib
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import TextIO

__version__ = "1.2.0"

USER_AGENT = f"geo-download/{__version__} (+https://github.com/cumakurt/geoip)"
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

# Order in which sources are tried when the source is auto-selected.
AUTO_SOURCE_ORDER = ("ipverse", "ipdeny")
FORMATS = ("plain", "ipset", "nft", "nginx", "json")
SET_FORMATS = frozenset({"ipset", "nft"})

COUNTRY_CODE_PATTERN = re.compile(r"^[A-Za-z]{2}$")
PERMANENT_HTTP_STATUS = frozenset({400, 401, 403, 404})

# ISO 3166-1 alpha-2 codes. Validated locally because ipdeny.com answers
# unknown country codes with HTTP 200 and the full IPv4 table, which would
# silently produce a wrong result.
# Country codes are kept as a whitespace-separated table because a 249-entry
# list literal is far harder to scan and to diff.
_ISO_COUNTRY_TABLE = """
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
"""
ISO_COUNTRY_CODES = frozenset(_ISO_COUNTRY_TABLE.split())

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
DIFF_MAX_LINES = 20

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNCHANGED = 3
EXIT_INTERRUPTED = 130

CACHE_BODY_SUFFIX = ".body"
CACHE_META_SUFFIX = ".json"


class DownloadError(RuntimeError):
    """Raised when no source could provide data for the requested country."""


# --------------------------------------------------------------------------- #
# Console UI
# --------------------------------------------------------------------------- #


class Console:
    """Terminal UI written to stderr, with color and Unicode degradation."""

    def __init__(
        self,
        stream: TextIO | None = None,
        quiet: bool = False,
        show_progress: bool = True,
    ) -> None:
        self.stream = stream or sys.stderr
        self.quiet = quiet
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.uses_color = (
            is_tty and "NO_COLOR" not in os.environ and os.environ.get("TERM", "") != "dumb"
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

    def info(self, text: str) -> None:
        self.line(f"  {text}")

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
        self.downloaded = 0
        self.finished_at = None
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
            filled = round(bar_fraction * bar_width)
            bar = console.style(console.bar_full * filled, "accent") + console.style(
                console.bar_empty * (bar_width - filled), "muted"
            )

        return f"  {label} {leading} {bar}  {console.style(plain_details, 'muted')}"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


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


def report_error(message: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    text = f"error: {message}"
    if getattr(stream, "isatty", lambda: False)() and "NO_COLOR" not in os.environ:
        text = f"{ANSI_STYLES['error']}{text}{ANSI_RESET}"
    print(text, file=stream)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="geo-download",
        description=(
            "Download a country's IP blocks, aggregate them into CIDR subnets\n"
            "and save them as <country>.txt in the current directory."
        ),
        epilog=(
            "Examples:\n"
            "  geo-download tr\n"
            "  geo-download tr de us\n"
            "  geo-download de --family both\n"
            "  geo-download us -o /etc/firewall/us.txt --no-progress\n"
            "  geo-download tr --format nft --stdout\n"
            "  geo-download tr --exclude 203.0.113.0/24 --if-changed\n"
            "  geo-download --check tr.txt\n"
            "  geo-download --completion bash > /etc/bash_completion.d/geo-download\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "countries",
        nargs="*",
        metavar="COUNTRY",
        help="ISO 3166-1 alpha-2 country code(s), for example 'tr' or 'tr de us'.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Output file. Default: <country>.txt. Only valid for a single country.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write the rendered list to stdout instead of a file. Only valid for a single country.",
    )

    family_group = parser.add_mutually_exclusive_group()
    family_group.add_argument(
        "--family",
        choices=("4", "6", "both"),
        default="4",
        help="Address family to download (default: 4).",
    )
    family_group.add_argument(
        "--ipv6",
        action="store_true",
        help="Include IPv6 blocks as well (alias for --family both).",
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
        "--format",
        choices=FORMATS,
        default="plain",
        help="Output format (default: plain).",
    )
    parser.add_argument(
        "--set-name",
        metavar="NAME",
        help="Set name used by --format ipset (default: the country code).",
    )
    parser.add_argument(
        "--with-header",
        action="store_true",
        help="Prepend comment lines with version, country, source and timestamp.",
    )

    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="CIDR",
        help="Remove a network from the result. May be repeated.",
    )
    parser.add_argument(
        "--exclude-file",
        metavar="PATH",
        help="File with networks to remove, one per line ('#' comments allowed).",
    )
    parser.add_argument(
        "--min-prefix",
        type=int,
        metavar="N",
        help="Drop networks broader than /N (prefix length less than N).",
    )
    parser.add_argument(
        "--max-prefix",
        type=int,
        metavar="N",
        help="Drop networks smaller than /N (prefix length greater than N).",
    )

    parser.add_argument(
        "--if-changed",
        action="store_true",
        help="Only replace the output file when its content actually changed.",
    )
    parser.add_argument(
        "--exit-code-changed",
        action="store_true",
        help=f"With --if-changed, exit {EXIT_UNCHANGED} when the file was left untouched.",
    )
    parser.add_argument(
        "--diff",
        metavar="PATH",
        help="Print networks added or removed compared to PATH before writing.",
    )

    parser.add_argument(
        "--check",
        metavar="PATH",
        help="Validate an existing block list offline and exit.",
    )
    parser.add_argument(
        "--cache-dir",
        metavar="PATH",
        help="Enable an HTTP cache with conditional requests in PATH.",
    )
    parser.add_argument(
        "--completion",
        choices=("bash", "zsh"),
        help="Print a shell completion script to stdout and exit.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress bar (also disabled when stderr is not a TTY).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all informational output on stderr.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(argv)

    if args.completion:
        if args.countries:
            parser.error("--completion does not take a country argument")
        return args

    if args.check:
        if args.countries:
            parser.error("--check does not take a country argument")
        return args

    if not args.countries:
        parser.error("at least one country code is required")

    args.countries = [normalize_country(parser, code) for code in args.countries]

    if args.output and len(args.countries) > 1:
        parser.error("-o/--output is only valid for a single country; omit it to write <country>.txt per country")
    if args.stdout and len(args.countries) > 1:
        parser.error("--stdout is only valid for a single country")
    if args.output and args.stdout:
        parser.error("use either -o/--output or --stdout, not both")
    if args.diff and len(args.countries) > 1:
        parser.error("--diff is only valid for a single country")

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    if args.set_name and args.format != "ipset":
        parser.error("--set-name is only valid with --format ipset")
    if args.with_header and args.format == "json":
        parser.error("--with-header is not supported by --format json")
    if args.if_changed and args.with_header:
        parser.error("--if-changed cannot be combined with --with-header (the header contains a timestamp)")
    if args.if_changed and args.stdout:
        parser.error("--if-changed has no effect with --stdout")
    if args.exit_code_changed and not args.if_changed:
        parser.error("--exit-code-changed requires --if-changed")
    if args.format in SET_FORMATS and selected_families(args) == (4, 6):
        # ipset and nft sets are typed by address family, so mixing both in a
        # single rendered set would produce config that cannot be applied.
        parser.error(
            f"--format {args.format} supports a single address family; "
            "use --family 4 or --family 6"
        )

    for name in ("min_prefix", "max_prefix"):
        value = getattr(args, name)
        if value is not None and not 0 <= value <= 128:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 128")
    if (
        args.min_prefix is not None
        and args.max_prefix is not None
        and args.min_prefix > args.max_prefix
    ):
        parser.error("--min-prefix must not be greater than --max-prefix")

    args.exclusions = collect_exclusions(parser, args)
    return args


def normalize_country(parser: argparse.ArgumentParser, code: str) -> str:
    if not COUNTRY_CODE_PATTERN.match(code):
        parser.error(f"invalid country code: {code!r} (expected two letters, e.g. 'tr')")
    country = code.upper()
    if country not in ISO_COUNTRY_CODES:
        parser.error(f"unknown country code: {country!r} (expected an ISO 3166-1 alpha-2 code)")
    return country


def selected_families(args: argparse.Namespace) -> tuple[int, ...]:
    if args.ipv6 or args.family == "both":
        return (4, 6)
    return (int(args.family),)


# --------------------------------------------------------------------------- #
# Network helpers
# --------------------------------------------------------------------------- #


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


def collapse_networks(networks: list) -> list:
    """Merge adjacent and overlapping networks into the smallest set.

    ``ipaddress.collapse_addresses`` rejects mixed address families, so each
    family is collapsed separately and the results are concatenated.
    """
    collapsed: list = []
    for version in (4, 6):
        group = [network for network in networks if network.version == version]
        if group:
            collapsed.extend(ipaddress.collapse_addresses(group))
    collapsed.sort(key=lambda network: (network.version, int(network.network_address), network.prefixlen))
    return collapsed


def load_network_file(path: str) -> list:
    """Load CIDR networks from a file, skipping blanks and '#' comments."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    networks = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        networks.append(ipaddress.ip_network(candidate, strict=False))
    return networks


def apply_prefix_filters(
    networks: list,
    min_prefix: int | None,
    max_prefix: int | None,
    family: int,
    console: Console,
) -> list:
    """Drop networks outside the requested prefix length range."""
    if min_prefix is None and max_prefix is None:
        return networks

    kept = []
    dropped = 0
    for network in networks:
        if min_prefix is not None and network.prefixlen < min_prefix:
            dropped += 1
            continue
        if max_prefix is not None and network.prefixlen > max_prefix:
            dropped += 1
            continue
        kept.append(network)

    if dropped:
        bounds = []
        if min_prefix is not None:
            bounds.append(f"/{min_prefix}")
        if max_prefix is not None:
            bounds.append(f"/{max_prefix}")
        console.warn(f"IPv{family}: dropped {dropped} networks outside {'..'.join(bounds)}")
    return kept


def apply_exclusions(
    networks: list,
    exclusions: list,
    family: int,
    matched: set,
) -> list:
    """Subtract exclusions from networks, tracking which ones matched anything."""
    relevant = [exclusion for exclusion in exclusions if exclusion.version == family]
    if not relevant:
        return networks

    result = []
    for network in networks:
        pieces = [network]
        for exclusion in relevant:
            next_pieces = []
            for piece in pieces:
                if not exclusion.overlaps(piece):
                    next_pieces.append(piece)
                    continue
                matched.add(exclusion)
                if exclusion == piece or exclusion.supernet_of(piece):
                    # The whole piece is excluded.
                    continue
                # The exclusion sits strictly inside the piece.
                next_pieces.extend(piece.address_exclude(exclusion))
            pieces = next_pieces
            if not pieces:
                break
        result.extend(pieces)
    return result


def parse_exclusion(parser: argparse.ArgumentParser, text: str) -> object:
    candidate = text.strip()
    if not candidate:
        parser.error("empty network in --exclude/--exclude-file")
    try:
        return ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        parser.error(f"invalid network in --exclude/--exclude-file: {candidate!r}")


def collect_exclusions(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list:
    """Build the deduplicated exclusion list from --exclude and --exclude-file."""
    values = list(args.exclude)
    if args.exclude_file:
        try:
            with open(args.exclude_file, encoding="utf-8") as handle:
                for line in handle:
                    candidate = line.split("#", 1)[0].strip()
                    if candidate:
                        values.append(candidate)
        except OSError as error:
            parser.error(f"cannot read --exclude-file {args.exclude_file}: {error}")

    excluded: dict[str, object] = {}
    for value in values:
        network = parse_exclusion(parser, value)
        excluded[str(network)] = network
    return list(excluded.values())


# --------------------------------------------------------------------------- #
# Download and cache
# --------------------------------------------------------------------------- #


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

    return b"".join(chunks)


def cache_paths(cache_dir: str, url: str) -> tuple[str, str]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return (
        os.path.join(cache_dir, digest + CACHE_BODY_SUFFIX),
        os.path.join(cache_dir, digest + CACHE_META_SUFFIX),
    )


def load_cache_entry(cache_dir: str, url: str) -> tuple[dict, bytes] | None:
    """Return ``(metadata, body)`` for a cached URL, or ``None``."""
    body_path, meta_path = cache_paths(cache_dir, url)
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        with open(body_path, "rb") as handle:
            body = handle.read()
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("url") != url or not body:
        return None
    return meta, body


def store_cache_entry(cache_dir: str, url: str, meta: dict, body: bytes) -> None:
    """Persist a cache entry, ignoring failures because the cache is optional."""
    body_path, meta_path = cache_paths(cache_dir, url)
    entry = {"url": url, "etag": meta.get("etag"), "last_modified": meta.get("last_modified")}
    try:
        os.makedirs(cache_dir, exist_ok=True)
        write_atomically(body_path, body, binary=True)
        write_atomically(meta_path, json.dumps(entry, indent=2).encode("utf-8"), binary=True)
    except OSError:
        # A broken cache must never break the download itself.
        pass


def fetch_bytes(
    url: str,
    timeout: float,
    progress: DownloadProgress | None = None,
    cache_dir: str | None = None,
) -> bytes:
    """Download ``url`` as bytes, retrying transient failures with backoff."""
    cached = load_cache_entry(cache_dir, url) if cache_dir else None
    last_error: Exception | None = None

    for attempt in range(RETRY_ATTEMPTS):
        headers = {"User-Agent": USER_AGENT, "Accept": "text/plain, */*"}
        if cached:
            meta, _body = cached
            if meta.get("etag"):
                headers["If-None-Match"] = str(meta["etag"])
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = str(meta["last_modified"])

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = read_body(response, progress=progress)
                if cache_dir:
                    store_cache_entry(
                        cache_dir,
                        url,
                        {
                            "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                        },
                        body,
                    )
                return body
        except urllib.error.HTTPError as error:
            status = error.code
            # HTTPError owns the socket of the failed response; release it
            # before deciding whether the request should be retried.
            error.close()
            if status == 304 and cached is not None:
                return cached[1]
            if status in PERMANENT_HTTP_STATUS:
                raise DownloadError(f"{url} returned HTTP {status}") from error
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


def fetch_text(
    url: str,
    timeout: float,
    progress: DownloadProgress | None = None,
    cache_dir: str | None = None,
) -> str:
    return fetch_bytes(url, timeout, progress, cache_dir).decode("utf-8", errors="replace")


def download_country(
    country: str,
    family: int,
    args: argparse.Namespace,
    console: Console,
) -> tuple[list, str, DownloadProgress]:
    """Return networks for one address family from the first usable source."""
    sources = AUTO_SOURCE_ORDER if args.source == "auto" else (args.source,)
    errors: list[str] = []

    for index, candidate in enumerate(sources):
        url = SOURCES[(candidate, family)].format(cc=country.lower())
        progress = DownloadProgress(console, f"IPv{family} {console.bullet} {candidate}")
        try:
            text = fetch_text(url, args.timeout, progress, args.cache_dir)
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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def build_header(country: str, source_names: list[str], networks: list) -> list[str]:
    ipv4 = sum(1 for network in networks if network.version == 4)
    ipv6 = len(networks) - ipv4
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        f"# generated by geo-download {__version__}",
        f"# country: {country}",
        f"# source: {', '.join(dict.fromkeys(source_names)) or 'unknown'}",
        f"# generated: {generated}",
        f"# ipv4: {ipv4} subnets",
        f"# ipv6: {ipv6} subnets",
        f"# total: {len(networks)} subnets",
    ]


def render_networks(
    networks: list,
    output_format: str,
    set_name: str = "",
    header: list[str] | None = None,
) -> list[str]:
    """Render networks into output lines for the requested format."""
    header = header or []

    if output_format == "plain":
        return header + [str(network) for network in networks]

    if output_format == "ipset":
        return header + [f"add {set_name} {network}" for network in networks]

    if output_format == "nginx":
        return header + [f"allow {network};" for network in networks]

    if output_format == "nft":
        if not networks:
            return [*header, "{", "}"]
        body = [f"    {network}," for network in networks]
        return [*header, "{", *body, "}"]

    if output_format == "json":
        ipv4 = [str(network) for network in networks if network.version == 4]
        ipv6 = [str(network) for network in networks if network.version == 6]
        payload = {
            "ipv4": ipv4,
            "ipv4_count": len(ipv4),
            "ipv6": ipv6,
            "ipv6_count": len(ipv6),
            "address_count": sum(network.num_addresses for network in networks),
            "total_count": len(networks),
        }
        return json.dumps(payload, indent=2).splitlines()

    raise ValueError(f"unsupported format: {output_format}")


def render_country(
    country: str,
    networks: list,
    args: argparse.Namespace,
    source_names: list[str],
) -> list[str]:
    header = build_header(country, source_names, networks) if args.with_header else None
    set_name = args.set_name or country.lower()
    return render_networks(networks, args.format, set_name=set_name, header=header)


# --------------------------------------------------------------------------- #
# Diff, check and write
# --------------------------------------------------------------------------- #


def diff_networks(console: Console, existing: list, current: list) -> tuple[int, int]:
    """Print added/removed networks compared to ``existing``."""
    existing_set = {str(network) for network in existing}
    current_set = {str(network) for network in current}

    added = sorted(
        current_set - existing_set,
        key=lambda text: (":" in text, ipaddress.ip_network(text)),
    )
    removed = sorted(
        existing_set - current_set,
        key=lambda text: (":" in text, ipaddress.ip_network(text)),
    )

    for text in added[:DIFF_MAX_LINES]:
        console.line(f"  {console.style('+', 'ok')} {text}")
    if len(added) > DIFF_MAX_LINES:
        console.line(f"  {console.style('+', 'ok')} ... ({len(added) - DIFF_MAX_LINES} more)")
    for text in removed[:DIFF_MAX_LINES]:
        console.line(f"  {console.style('-', 'error')} {text}")
    if len(removed) > DIFF_MAX_LINES:
        console.line(f"  {console.style('-', 'error')} ... ({len(removed) - DIFF_MAX_LINES} more)")

    if added or removed:
        console.info(
            f"diff: {console.style(f'+{len(added)}', 'ok')} "
            f"{console.style(f'-{len(removed)}', 'error')}"
        )
    else:
        console.info("diff: no changes")
    return len(added), len(removed)


def check_file(path: str, console: Console) -> int:
    """Validate an existing block list without touching the network."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        report_error(f"cannot read {path}: {error}")
        return EXIT_ERROR

    invalid: list[tuple[int, str]] = []
    non_canonical: list[tuple[int, str]] = []
    duplicates: list[tuple[int, str]] = []
    entries: list[tuple[int, object]] = []
    seen: set[str] = set()

    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            invalid.append((number, text))
            continue
        canonical = str(network)
        if canonical != text:
            non_canonical.append((number, text))
        if canonical in seen:
            duplicates.append((number, canonical))
            continue
        seen.add(canonical)
        entries.append((number, network))

    # Overlap detection must not depend on the order of the file, so it runs on
    # a sorted copy while the ordering check uses the original order.
    overlaps = 0
    previous = None
    for _number, network in sorted(
        entries, key=lambda item: (item[1].version, int(item[1].network_address), item[1].prefixlen)
    ):
        if previous is not None and previous.version == network.version:
            if int(network.network_address) <= int(previous.broadcast_address):
                overlaps += 1
            if int(network.broadcast_address) > int(previous.broadcast_address):
                previous = network
        else:
            previous = network

    unsorted = 0
    previous = None
    for _number, network in entries:
        if (
            previous is not None
            and previous.version == network.version
            and (int(network.network_address), network.prefixlen)
            < (int(previous.network_address), previous.prefixlen)
        ):
            unsorted += 1
        previous = network

    ipv4 = sum(1 for _number, network in entries if network.version == 4)
    ipv6 = len(entries) - ipv4

    console.title(f"check {path}")
    console.info(
        f"{len(entries):,} networks {console.bullet} IPv4 {ipv4:,} {console.bullet} IPv6 {ipv6:,}"
    )

    for number, text in invalid[:DIFF_MAX_LINES]:
        console.line(f"  {console.style('!', 'error')} line {number}: not a network: {text}")
    if len(invalid) > DIFF_MAX_LINES:
        console.line(f"  {console.style('!', 'error')} ... ({len(invalid) - DIFF_MAX_LINES} more)")
    for number, text in duplicates[:DIFF_MAX_LINES]:
        console.line(f"  {console.style('!', 'error')} line {number}: duplicate: {text}")
    if len(duplicates) > DIFF_MAX_LINES:
        console.line(f"  {console.style('!', 'error')} ... ({len(duplicates) - DIFF_MAX_LINES} more)")
    for number, text in non_canonical[:DIFF_MAX_LINES]:
        console.warn(f"line {number}: not canonical: {text}")
    if overlaps:
        console.warn(f"{overlaps} overlapping network(s); run through geo-download to collapse")
    if unsorted:
        console.warn(f"{unsorted} network(s) out of order")
    if not entries:
        console.warn("no networks found")

    problems = len(invalid) + len(duplicates) + overlaps
    if problems:
        console.line()
        console.line(f"  {console.style('failed', 'error')} {console.bullet} {problems} problem(s)")
        console.line()
        return EXIT_ERROR

    console.success("ok")
    console.line()
    return EXIT_OK


def write_atomically(path: str, data, binary: bool = False) -> None:
    """Write ``data`` to ``path`` via a temporary file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    descriptor, temporary_path = tempfile.mkstemp(
        dir=directory, prefix=".geo_download-", suffix=".tmp"
    )
    try:
        if binary:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        else:
            lines = data if isinstance(data, list) else [data]
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                for line in lines:
                    handle.write(line)
                    handle.write("\n")
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except BaseException:
        _discard(temporary_path)
        raise


def _discard(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def file_content_matches(path: str, lines: list[str]) -> bool:
    """Return True when ``path`` already holds exactly ``lines``."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read() == "\n".join(lines) + "\n"
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Shell completion
# --------------------------------------------------------------------------- #

COMPLETION_OPTIONS = (
    "--output",
    "--stdout",
    "--family",
    "--ipv6",
    "--source",
    "--timeout",
    "--format",
    "--set-name",
    "--with-header",
    "--exclude",
    "--exclude-file",
    "--min-prefix",
    "--max-prefix",
    "--if-changed",
    "--exit-code-changed",
    "--diff",
    "--check",
    "--cache-dir",
    "--completion",
    "--no-progress",
    "--quiet",
    "--version",
    "--help",
)

BASH_COMPLETION_TEMPLATE = """\
# bash completion for geo-download
# Install with: geo-download --completion bash > /etc/bash_completion.d/geo-download

_geo_download_countries="{countries}"

_geo_download() {{
    local cur prev
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    case "${{prev}}" in
        -o|--output|--diff|--check|--cache-dir|--exclude-file)
            COMPREPLY=( $(compgen -f -- "${{cur}}") )
            return 0
            ;;
        --source)
            COMPREPLY=( $(compgen -W "auto ipverse ipdeny" -- "${{cur}}") )
            return 0
            ;;
        --format)
            COMPREPLY=( $(compgen -W "plain ipset nft nginx json" -- "${{cur}}") )
            return 0
            ;;
        --family)
            COMPREPLY=( $(compgen -W "4 6 both" -- "${{cur}}") )
            return 0
            ;;
        --completion)
            COMPREPLY=( $(compgen -W "bash zsh" -- "${{cur}}") )
            return 0
            ;;
        --exclude)
            COMPREPLY=( $(compgen -W "$(ip -o addr show 2>/dev/null | awk '{{print $4}}')" -- "${{cur}}") )
            return 0
            ;;
    esac

    if [[ "${{cur}}" == -* ]]; then
        COMPREPLY=( $(compgen -W "{options}" -- "${{cur}}") )
        return 0
    fi

    COMPREPLY=( $(compgen -W "${{_geo_download_countries}}" -- "${{cur}}") )
}}

complete -F _geo_download geo-download geo_download.py
"""

ZSH_COMPLETION_TEMPLATE = """\
#compdef geo-download geo_download.py
# Install with: geo-download --completion zsh > "${{fpath[1]}}/_geo-download"

_geo_download_countries() {{
    compadd {countries}
}}

_geo_download() {{
    _arguments -s \\
        '(-o --output)'{{-o,--output}}'[output file]:file:_files' \\
        '--stdout[write the rendered list to stdout]' \\
        '--family[address family]:family:(4 6 both)' \\
        '--ipv6[include IPv6 blocks]' \\
        '--source[data source]:source:(auto ipverse ipdeny)' \\
        '--timeout[network timeout in seconds]:seconds:' \\
        '--format[output format]:format:(plain ipset nft nginx json)' \\
        '--set-name[ipset set name]:name:' \\
        '--with-header[prepend metadata comments]' \\
        '*--exclude[remove a network]:network:' \\
        '--exclude-file[file with networks to remove]:file:_files' \\
        '--min-prefix[drop broader networks]:prefix:' \\
        '--max-prefix[drop smaller networks]:prefix:' \\
        '--if-changed[only write when content changed]' \\
        '--exit-code-changed[exit 3 when unchanged]' \\
        '--diff[show changes compared to a file]:file:_files' \\
        '--check[validate an existing file]:file:_files' \\
        '--cache-dir[HTTP cache directory]:directory:_directories' \\
        '--completion[print completion script]:shell:(bash zsh)' \\
        '--no-progress[disable the progress bar]' \\
        '--quiet[suppress informational output]' \\
        '--version[show version]' \\
        '--help[show help]' \\
        '*:country code:_geo_download_countries'
}}

_geo_download "$@"
"""


def render_completion(shell: str) -> str:
    countries = " ".join(sorted(code.lower() for code in ISO_COUNTRY_CODES))
    if shell == "bash":
        return BASH_COMPLETION_TEMPLATE.format(
            countries=countries,
            options=" ".join(COMPLETION_OPTIONS),
        )
    if shell == "zsh":
        return ZSH_COMPLETION_TEMPLATE.format(countries=countries)
    raise ValueError(f"unsupported shell: {shell}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def resolve_output_path(country: str, args: argparse.Namespace) -> str:
    return args.output or f"{country.lower()}.txt"


def process_country(
    country: str,
    args: argparse.Namespace,
    exclusions: list,
    matched_exclusions: set,
    console: Console,
) -> tuple[list[str], list[str], bool]:
    """Download and render one country. Returns (lines, sources, wrote)."""
    networks: list = []
    source_names: list[str] = []
    started = time.monotonic()

    for family in selected_families(args):
        raw_networks, used_source, progress = download_country(country, family, args, console)
        source_names.append(used_source)

        filtered = apply_prefix_filters(
            raw_networks, args.min_prefix, args.max_prefix, family, console
        )
        excluded = apply_exclusions(filtered, exclusions, family, matched_exclusions)
        collapsed = collapse_networks(excluded)
        networks.extend(collapsed)

        address_count = sum(network.num_addresses for network in collapsed)
        console.success(
            f"{country} {console.bullet} IPv{family} {console.bullet} {used_source} {console.bullet} "
            f"{len(collapsed):,} subnets {console.bullet} {address_count:,} addresses "
            f"{console.bullet} {format_bytes(progress.downloaded)} in "
            f"{format_duration(progress.elapsed)}"
        )

    networks = collapse_networks(networks)
    if not networks:
        raise DownloadError(f"no networks left for {country} after filtering and exclusions")

    if args.diff:
        try:
            existing = load_network_file(args.diff)
        except OSError as error:
            console.warn(f"cannot read --diff file {args.diff}: {error}")
            existing = []
        except ValueError as error:
            console.warn(f"cannot parse --diff file {args.diff}: {error}")
            existing = []
        diff_networks(console, existing, networks)

    lines = render_country(country, networks, args, source_names)

    if args.stdout:
        for line in lines:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        console.info(f"wrote {len(lines):,} lines to stdout {console.bullet} {format_duration(time.monotonic() - started)}")
        return lines, source_names, True

    output_path = resolve_output_path(country, args)
    if args.if_changed and file_content_matches(output_path, lines):
        console.info(f"{output_path} {console.bullet} unchanged {console.bullet} {len(networks):,} subnets")
        return lines, source_names, False

    try:
        write_atomically(output_path, lines)
    except OSError as error:
        raise DownloadError(f"cannot write {output_path}: {error}") from error

    console.line()
    console.line(
        f"  {console.style(console.arrow, 'accent')} "
        f"{console.style(os.path.abspath(output_path), 'bold')} "
        f"{console.bullet} {len(networks):,} subnets "
        f"{console.bullet} {format_duration(time.monotonic() - started)}"
    )
    console.line()
    return lines, source_names, True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.completion:
        sys.stdout.write(render_completion(args.completion))
        sys.stdout.flush()
        return EXIT_OK

    console = Console(quiet=args.quiet, show_progress=not args.no_progress)

    if args.check:
        return check_file(args.check, console)

    exclusions = args.exclusions
    matched_exclusions: set = set()

    if len(args.countries) > 1:
        console.title(f"geo-download {console.bullet} {' '.join(args.countries)}")
    else:
        console.title(f"geo-download {console.bullet} {args.countries[0]}")

    wrote_any = False
    try:
        for country in args.countries:
            _lines, _sources, wrote = process_country(
                country, args, exclusions, matched_exclusions, console
            )
            wrote_any = wrote_any or wrote
    except DownloadError as error:
        report_error(str(error))
        return EXIT_ERROR
    except KeyboardInterrupt:
        console.clear_progress()
        report_error("interrupted")
        return EXIT_INTERRUPTED

    unused = [str(network) for network in exclusions if network not in matched_exclusions]
    if unused:
        console.warn(f"{len(unused)} exclusion(s) matched no downloaded network: {', '.join(unused[:5])}")
        console.line()

    if args.exit_code_changed and not wrote_any:
        return EXIT_UNCHANGED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
