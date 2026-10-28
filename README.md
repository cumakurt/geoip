# geo-download

A small, dependency-free command line tool that downloads the IP address blocks
of a country, merges them into the smallest possible set of CIDR subnets and
saves the result as `<country>.txt` in the current directory.

```console
$ python3 geo_download.py tr

  geo-download · TR

  IPv4 · ipverse          ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  100%  25.0 KB / 25.0 KB  1.8 MB/s
  ✔ IPv4 · ipverse · 1,626 subnets · 16,826,048 addresses · 25.0 KB in 14 ms

  → /home/user/geoip/tr.txt · 1,626 subnets · 199 ms
```

The output is a plain text file with one CIDR subnet per line, ready to be fed
into `ipset`, `nftables`, `iptables`, nginx `allow`/`deny` rules, fail2ban,
HAProxy ACLs or any other tool that accepts a network list.

- **Author:** Cuma Kurt (`cumakurt@gmail.com`)
- **License:** GNU General Public License v3.0 or later (GPL-3.0-or-later)

## Features

- **Zero dependencies** — only the Python standard library.
- **Aggregated output** — adjacent and overlapping blocks are collapsed, so the
  file contains the minimum number of subnets that cover the same address space.
- **Two data sources with automatic fallback** — `ipverse/rir-ip` first,
  `ipdeny.com` as a backup.
- **Live progress bar** — byte-accurate when the server reports a
  `Content-Length`, spinner with transfer rate otherwise. Displays speed and ETA.
- **Modern console output** — colors, Unicode glyphs and automatic degradation:
  colors are disabled when stdout is not a TTY, `NO_COLOR` is set or
  `TERM=dumb`, and ASCII glyphs are used when the terminal encoding cannot
  represent the Unicode ones.
- **Safe writes** — the file is written to a temporary file in the target
  directory and atomically renamed, so a failed run never leaves a truncated
  list behind.
- **Predictable failure handling** — timeouts, bounded retries with exponential
  backoff and jitter, hard 32 MiB download limit, no retry on permanent HTTP
  errors (4xx).
- **Validated input** — country codes are checked against the ISO 3166-1
  alpha-2 list before any request is made.

## Requirements

- Python 3.8 or newer (developed and tested with Python 3.14).
- Outbound HTTPS access to `raw.githubusercontent.com` and/or `ipdeny.com`.
- No third-party packages, no build step.

## Installation

```bash
chmod +x geo_download.py
```

Optionally put it on your `PATH`:

```bash
install -m 0755 geo_download.py ~/.local/bin/geo-download
```

Then use it as `geo-download tr`.

## Usage

```bash
# IPv4 blocks of Türkiye -> ./tr.txt
python3 geo_download.py tr

# IPv4 and IPv6 blocks of Germany -> ./de.txt
python3 geo_download.py de --ipv6

# Explicit output path, no colors or progress bar (cron / CI friendly)
python3 geo_download.py us -o /etc/firewall/us.txt --no-progress

# Force a specific source
python3 geo_download.py fr --source ipdeny

# Machine-readable: only the exit code matters
python3 geo_download.py jp --quiet
```

Country codes are case-insensitive: `tr`, `TR` and `Tr` are equivalent.

### Options

| Option | Description |
| --- | --- |
| `country` | Required. ISO 3166-1 alpha-2 country code (`tr`, `de`, `us`, …). |
| `-o`, `--output PATH` | Output file. Default: `<country>.txt` (lowercase) in the current directory. Missing parent directories are created. |
| `--ipv6` | Also download IPv6 blocks and write them into the same file, after the IPv4 entries. |
| `--source {auto,ipverse,ipdeny}` | Data source. `auto` (default) tries `ipverse`, then `ipdeny`. |
| `--timeout SECONDS` | Network timeout per request. Default: `30`. Must be greater than zero. |
| `--no-progress` | Disable the progress bar. It is also disabled automatically when stdout is not a TTY. |
| `--quiet` | Suppress all informational output on stdout. Errors still go to stderr. |
| `-h`, `--help` | Show usage and exit. |

### Progress bar

The bar is drawn on a single line and refreshed at most every 80 ms:

```
  IPv4 · ipverse          ━━━━━━━━━━━━━━━━━  62%  15.4 KB / 25.0 KB  1.9 MB/s  eta 00:00
```

- When the server sends `Content-Length`, the bar shows the exact percentage,
  transferred/total bytes, transfer rate (averaged over the request) and ETA.
- When the length is unknown (chunked responses), a spinner and the byte count
  keep the user informed instead of a fake percentage.
- Each finished download replaces the bar with a one-line result, so long runs
  leave a readable log rather than a screen full of stale frames.

The bar is printed only when stdout is an interactive terminal, so piping,
redirecting to a file or running from cron never produces escape sequences.
`--no-progress` disables it explicitly, `--quiet` disables all UI output.

## Output format

- UTF-8, LF line endings, one subnet per line.
- No comments, no headers, no blank lines.
- Valid CIDR notation in canonical form (`2.56.60.0/22`, not `2.56.60.0/255.255.252.0`).
- Deduplicated, merged and sorted: IPv4 blocks first, then IPv6 blocks, each
  ordered by network address and prefix length.
- File mode `0644`.

Example:

```text
2.56.60.0/22
2.56.152.0/22
2.57.188.0/22
2.57.212.0/23
2.57.238.0/24
...
```

Because the list is already aggregated, it is safe to consume directly as a set
of networks; no further merging is required on the consumer side.

## How it works

1. **Validate** the country code locally against the ISO 3166-1 alpha-2 list.
2. **Download** the zone file for the requested address family from the first
   source that responds successfully.
3. **Parse** each line as a CIDR network. Comment lines (`#`) and anything that
   does not parse as a network of the requested address family are skipped —
   some providers occasionally include headers or hostnames.
4. **Collapse** the networks with `ipaddress.collapse_addresses()`, which
   removes duplicates and merges adjacent or overlapping blocks into the
   smallest equivalent set, then sort the result.
5. **Write** the list to a temporary file next to the target and `os.replace()`
   it into place.

## Data sources

| Source | URL pattern | Notes |
| --- | --- | --- |
| `ipverse` (default) | `https://raw.githubusercontent.com/ipverse/rir-ip/master/country/<cc>/ipv4-aggregated.txt` (and `ipv6-aggregated.txt`) | Derived from RIR delegation files, already aggregated per country. Preferred because it reflects registry delegations and serves files with `Content-Length`. |
| `ipdeny` (fallback) | `https://www.ipdeny.com/ipblocks/data/aggregated/<cc>-aggregated.zone` and `https://www.ipdeny.com/ipv6/ipaddresses/aggregated/<cc>-aggregated.zone` | Community-maintained aggregated zone files, widely used by firewall scripts. |

Both sources are third-party services. The two lists are close but not always
identical (a few hundred subnets in either direction for large countries),
because they are generated from different snapshots and aggregation rules.
If you need reproducible results, pin the source with `--source` and keep your
own copy of the generated file.

### Why the country code is validated locally

`ipdeny.com` answers a request for an unknown country code (for example `zz`)
with **HTTP 200 and the complete IPv4 table** instead of an error. Without local
validation the tool would happily write a "country" file containing the whole
internet. The ISO check prevents that class of silent, very hard to notice bug.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success, file written. |
| `1` | No source could deliver data, or the output file could not be written. |
| `2` | Invalid command line arguments (bad country code, invalid timeout, unknown option). |
| `130` | Interrupted with `Ctrl+C`. |

Errors are printed to stderr with an `error:` prefix; normal output goes to
stdout. This makes `--quiet` runs safe in pipelines.

## Environment variables

| Variable | Effect |
| --- | --- |
| `NO_COLOR` | Any non-empty value disables ANSI colors. |
| `TERM` | `TERM=dumb` disables ANSI colors. |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | Honored by `urllib`, so the tool works behind a corporate proxy. |

## Automation

### cron

```cron
# Refresh the Turkish block list every day at 04:17
17 4 * * * cd /srv/geoip && /usr/bin/python3 geo_download.py tr --quiet && /usr/bin/nft -f /srv/geoip/apply.nft
```

Use `--quiet` in cron: the file is only replaced after a fully successful
download, so a network failure leaves the previous list untouched and the job
can simply be retried.

### systemd timer

```ini
# /etc/systemd/system/geo-tr.service
[Unit]
Description=Refresh Turkish IP block list

[Service]
Type=oneshot
WorkingDirectory=/srv/geoip
ExecStart=/usr/bin/python3 /srv/geoip/geo_download.py tr --quiet
```

```ini
# /etc/systemd/system/geo-tr.timer
[Unit]
Description=Daily Turkish IP block refresh

[Timer]
OnCalendar=daily
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

## Consuming the list

### ipset (netfilter)

```bash
ipset create tr4 hash:net family inet -exist
ipset flush tr4
sed 's/^/add tr4 /' tr.txt | ipset restore -exist
```

For IPv6, filter the file with `grep ':'` and create a `family inet6` set.

### nftables

Generate a comma-separated element list:

```bash
awk '{printf "%s%s", sep, $0; sep=", "} END {print ""}' tr.txt
```

and place it inside a set definition in your ruleset, or add elements
incrementally with `nft add element`.

### iptables

```bash
while read -r network; do
  iptables -A INPUT -s "$network" -j DROP
done < tr.txt
```

This is slow for lists with thousands of entries — prefer `ipset` or
`nftables` sets, which use hash lookups instead of a linear rule chain.

### nginx

`allow`/`deny` accept bare CIDRs, so generating a snippet keeps the config
readable:

```bash
sed 's/^/allow /; s/$/;/' tr.txt > /etc/nginx/snippets/tr.allow
```

```nginx
# /etc/nginx/conf.d/example.conf
server {
    include /etc/nginx/snippets/tr.allow;
    deny all;
}
```

## Limitations

- **Not a geolocation database.** The list answers "which networks are
  registered to this country", not "where is this IP physically located".
  Anycast, VPNs, satellite and mobile roaming traffic will not match user
  expectations, and registry delegations lag real-world reassignments.
- **Registry-level data, not per-ISP filtering.** Blocks that a provider
  announces outside the country are still included if they are delegated to it.
- **IPv4 by default.** IPv6 requires the explicit `--ipv6` flag.
- **No caching between runs.** Every invocation downloads fresh data; for a
  large fleet, run the tool once and distribute the resulting file.
- **The progress bar starts when the response body arrives.** DNS resolution,
  TCP connect and the TLS handshake are not reflected in the bar, so very slow
  connection setup looks like a stall before the first frame. `--timeout`
  covers each socket operation, not the total wall clock time.
- **32 MiB hard limit** per file, as a guard against a misbehaving server
  returning an unbounded stream. No country comes close to this.

## Troubleshooting

**`could not fetch IPv4 blocks for ...`** — every configured source failed.
The message contains the per-source reason. Check connectivity, proxy settings
and whether the source is temporarily unavailable; then retry or pin the other
source with `--source`.

**`unknown country code`** — the code is not in the ISO 3166-1 alpha-2 list.
Note that some codes exist in RIR data but not in ISO (`EU`, `AP`, `ZZ`), and
those are rejected by design.

**No progress bar** — stdout is not a TTY (piped, redirected, captured by a
process manager), `--no-progress` was passed, or `--quiet` is active.

**Numbers differ from another tool** — aggregation rules and snapshot dates
differ between providers. Compare the collapsed sets, not the line counts:
1623 and 1626 subnets can describe exactly the same address space.

## Development

The script is a single file. Quick sanity checks:

```bash
python3 -m py_compile geo_download.py   # syntax
python3 geo_download.py --help          # CLI surface
python3 geo_download.py tr --quiet -o /tmp/tr.txt
```

Validation of an existing list (no overlaps, correct ordering):

```bash
python3 - <<'PY'
import ipaddress

networks = [ipaddress.ip_network(line.strip()) for line in open("tr.txt") if line.strip()]
assert networks == sorted(networks, key=lambda n: (n.version, int(n.network_address), n.prefixlen))
assert all(int(a.broadcast_address) < int(b.network_address) for a, b in zip(networks, networks[1:]))
print(f"{len(networks)} subnets, no overlaps, sorted")
PY
```

## License

Copyright (C) 2026 Cuma Kurt <cumakurt@gmail.com>

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but **without
any warranty**; without even the implied warranty of merchantability or fitness
for a particular purpose. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

The full license text is available in the [`LICENSE`](LICENSE) file.

### Third-party data

The GPL covers this program's source code only. The IP block data downloaded at
runtime comes from third parties — `ipverse/rir-ip` (GitHub) and `ipdeny.com` —
and remains subject to their respective terms. Generated `.txt` files contain
only registry delegation data (RIR allocations), not code, so they are not
derived works of this program; redistribute them under whatever terms you need.

### Contributing

Contributions are accepted under the same license. By submitting a change you
agree that it may be distributed under GPL-3.0-or-later. Please keep the
existing style: standard library only, ASCII-only source code, English
identifiers/comments/messages, and no new runtime dependencies without a clear
justification.
