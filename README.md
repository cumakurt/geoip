# geo-download

A small, dependency-free command line tool that downloads the IP address blocks
of a country, merges them into the smallest possible set of CIDR subnets and
writes the result in a format your firewall can consume directly.

```console
$ geo-download tr

  geo-download · TR

  IPv4 · ipverse          ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  100%  25.0 KB / 25.0 KB  1.8 MB/s
  ✔ TR · IPv4 · ipverse · 1,626 subnets · 16,826,048 addresses · 25.0 KB in 14 ms

  → /home/user/geoip/tr.txt · 1,626 subnets · 199 ms
```

- **Author:** Cuma Kurt (`cumakurt@gmail.com`)
- **License:** GNU General Public License v3.0 or later (GPL-3.0-or-later)

## Features

- **Zero dependencies** — only the Python standard library, single module.
- **Aggregated output** — adjacent and overlapping blocks are collapsed, so the
  file contains the minimum number of subnets covering the same address space.
- **Five output formats** — `plain`, `ipset`, `nft`, `nginx` and `json`, so the
  list can go straight into a firewall without `sed`/`awk` glue.
- **Two data sources with automatic fallback** — `ipverse/rir-ip` first,
  `ipdeny.com` as a backup.
- **Live progress bar** — byte-accurate when the server reports
  `Content-Length`, spinner with transfer rate otherwise, plus speed and ETA.
- **Modern console output** — colors, Unicode glyphs and automatic degradation
  (no colors when stderr is not a TTY, `NO_COLOR` is set or `TERM=dumb`; ASCII
  glyphs when the terminal encoding cannot render the Unicode ones).
- **Safe writes** — the file is written to a temporary file in the target
  directory and atomically renamed, so a failed run never leaves a truncated
  list behind.
- **Incremental automation** — `--if-changed`, `--exit-code-changed` and
  `--diff` make cron and systemd jobs cheap and auditable.
- **Conditional HTTP cache** — `--cache-dir` revalidates with `ETag` /
  `Last-Modified`, so unchanged upstream files are not transferred again.
- **Offline validation** — `--check` verifies an existing list for parse errors,
  duplicates, overlaps and ordering.
- **Exclusions and prefix guards** — keep your own infrastructure out of the
  list and reject implausibly broad blocks.
- **Predictable failure handling** — timeouts, bounded retries with exponential
  backoff and jitter, hard 32 MiB download limit, no retry on permanent HTTP
  errors (4xx).
- **Validated input** — country codes are checked against the ISO 3166-1
  alpha-2 list before any request is made.
- **Shell completion and a man page** — `--completion bash|zsh` and
  `geo-download.1`.

## Requirements

- Python 3.8 or newer (developed and tested with Python 3.14).
- Outbound HTTPS access to `raw.githubusercontent.com` and/or `ipdeny.com`.
- No third-party packages, no build step.

## Installation

### pipx (recommended)

```bash
pipx install git+https://github.com/cumakurt/geoip
geo-download tr
```

### pip

```bash
pip install .
```

### Without installing

```bash
chmod +x geo_download.py
./geo_download.py tr
```

Or put the script on your `PATH`:

```bash
install -m 0755 geo_download.py ~/.local/bin/geo-download
```

### Shell completion and man page

```bash
geo-download --completion bash | sudo tee /etc/bash_completion.d/geo-download >/dev/null
geo-download --completion zsh > "${fpath[1]}/_geo-download"
man ./geo-download.1
```

## Usage

```bash
# IPv4 blocks of Türkiye -> ./tr.txt
geo-download tr

# Several countries, one file each (tr.txt, de.txt, us.txt)
geo-download tr de us

# IPv4 and IPv6 blocks of Germany
geo-download de --family both

# nftables element list on stdout, ready to paste into a set
geo-download tr --format nft --stdout

# ipset restore format, straight into a set
geo-download tr --format ipset --set-name tr4 --stdout | ipset restore -exist

# nginx allow snippet
geo-download tr --format nginx -o /etc/nginx/snippets/tr.allow

# JSON for further processing
geo-download tr --format json --stdout | jq '.ipv4 | length'

# Cron friendly: write only when the list actually changed
geo-download tr --quiet --if-changed --exit-code-changed

# Audit what a refresh would change
geo-download tr --diff /srv/geoip/tr.txt

# Validate an existing list offline
geo-download --check /srv/geoip/tr.txt
```

Country codes are case-insensitive: `tr`, `TR` and `Tr` are equivalent.

### Options

| Option | Description |
| --- | --- |
| `COUNTRY...` | One or more ISO 3166-1 alpha-2 country codes. |
| `-o`, `--output PATH` | Output file. Default: `<country>.txt`. Only valid for a single country. |
| `--stdout` | Write the rendered list to stdout instead of a file. Only valid for a single country. |
| `--family {4,6,both}` | Address family. Default: `4`. |
| `--ipv6` | Alias for `--family both`. |
| `--source {auto,ipverse,ipdeny}` | Data source. `auto` (default) tries `ipverse`, then `ipdeny`. |
| `--timeout SECONDS` | Socket timeout per request. Default: `30`. |
| `--format {plain,ipset,nft,nginx,json}` | Output format. Default: `plain`. |
| `--set-name NAME` | Set name for `--format ipset`. Default: the lowercase country code. |
| `--with-header` | Prepend comment lines (version, country, source, timestamp, counts). Not supported by `json`. |
| `--exclude CIDR` | Remove a network from the result. Repeatable. |
| `--exclude-file PATH` | Read networks to remove from a file. Blank lines and `#` comments are ignored. |
| `--min-prefix N` | Drop networks broader than `/N`. |
| `--max-prefix N` | Drop networks smaller than `/N`. |
| `--if-changed` | Leave the output file untouched when the content is identical. |
| `--exit-code-changed` | With `--if-changed`, exit `3` when nothing was written. |
| `--diff PATH` | Print networks added/removed compared to `PATH` before writing. |
| `--check PATH` | Validate an existing list offline and exit. |
| `--cache-dir PATH` | Enable a conditional HTTP cache in `PATH`. |
| `--completion {bash,zsh}` | Print a completion script to stdout and exit. |
| `--no-progress` | Disable the progress bar (also disabled when stderr is not a TTY). |
| `--quiet` | Suppress informational output. Errors are still reported. |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Show usage and exit. |

## Output formats

`plain` (default) — one subnet per line, nothing else:

```text
2.56.60.0/22
2.56.152.0/22
```

`ipset` — `ipset restore` input. `--set-name` defaults to the country code:

```text
add tr4 2.56.60.0/22
add tr4 2.56.152.0/22
```

`nft` — an nftables element list:

```text
{
    2.56.60.0/22,
    2.56.152.0/22,
}
```

Use it inside a set definition, for example
`nft add element inet filter tr4 '{ 2.56.60.0/22 }'` or by inlining the block
in your ruleset.

`nginx` — allow rules:

```text
allow 2.56.60.0/22;
allow 2.56.152.0/22;
```

`json` — stable and deterministic (no timestamp, so `--if-changed` works):

```json
{
  "ipv4": ["2.56.60.0/22", "2.56.152.0/22"],
  "ipv4_count": 2,
  "ipv6": [],
  "ipv6_count": 0,
  "address_count": 2048,
  "total_count": 2
}
```

`ipset` and `nft` sets are typed by address family, so combining both families
in one rendered set produces configuration that cannot be applied. The tool
refuses that combination instead of writing a broken file:

```console
$ geo-download tr --ipv6 --format ipset
geo-download: error: --format ipset supports a single address family; use --family 4 or --family 6
```

## Console output and the progress bar

**All human-readable output goes to stderr** (title, results, warnings, the
progress bar). Stdout carries data only when `--stdout` is given. This is the
same convention as `curl` and `wget`, and it means:

```bash
geo-download tr --stdout > tr.txt      # clean file, UI still visible
geo-download tr > /dev/null 2>log.txt  # UI in the log
```

The bar is drawn on a single line, refreshed at most every 80 ms:

```
  IPv4 · ipverse          ━━━━━━━━━━━━━━━━━  62%  15.4 KB / 25.0 KB  1.9 MB/s  eta 00:00
```

- With `Content-Length`, the bar shows exact percentage, transferred/total
  bytes, transfer rate and ETA.
- Without it (chunked responses), a spinner and the byte count replace the fake
  percentage.
- When a download finishes, the bar line is erased and replaced by a one-line
  result, so long runs leave a readable log instead of stale frames.

The bar is printed only when stderr is an interactive terminal. `--no-progress`
disables it explicitly, `--quiet` disables all UI output.

## Exclusions and prefix filters

Real deployments usually need to keep their own infrastructure out of a
country list, or guard against an implausibly broad block arriving from
upstream:

```bash
geo-download tr --exclude 203.0.113.0/24 --exclude 198.51.100.7
geo-download tr --exclude-file my-infrastructure.txt
geo-download tr --min-prefix 8 --max-prefix 24
```

- Exclusions are subtracted network by network with
  `ipaddress.address_exclude`, so a `/24` removed from a `/16` leaves the eight
  surrounding subnets, not a hole.
- An exclusion that matches nothing is reported, because it usually means a
  typo or the wrong file was passed.
- `--min-prefix` drops networks broader than `/N` (for example a stray `/4`),
  `--max-prefix` drops networks narrower than `/N` (single hosts). Both accept
  `0..128` and `--min-prefix` must not exceed `--max-prefix`.
- When both families are requested, the same bounds apply to IPv4 and IPv6, so
  choose values that make sense for both or run once per family.

## Incremental automation

```bash
geo-download tr --quiet --if-changed --exit-code-changed
```

| Situation | Exit code |
| --- | --- |
| File written (or `--stdout` used) | `0` |
| File already up to date, `--exit-code-changed` given | `3` |
| Download, filtering or write failure | `1` |
| Invalid arguments | `2` |
| Interrupted with `Ctrl+C` | `130` |

`--diff PATH` prints what would change before the file is written (at most 20
lines per side, plus a summary):

```console
$ geo-download tr --diff /srv/geoip/tr.txt
  + 2.58.40.0/22
  - 5.1.2.0/24
  diff: +1 -1
```

`--with-header` and `--if-changed` are mutually exclusive: the header contains a
generation timestamp, which would make every run look changed.

## Caching

```bash
geo-download tr --cache-dir ~/.cache/geo-download
```

The cache stores the response body and its `ETag`/`Last-Modified` validators per
URL. The next run sends `If-None-Match`/`If-Modified-Since`; a `304 Not
Modified` reuses the stored body, so an unchanged upstream file costs one small
request instead of a full download. The cache is best effort: a corrupt or
unwritable cache never breaks a download.

## Validating an existing list

```console
$ geo-download --check tr.txt

  check tr.txt

  1,626 networks · IPv4 1,626 · IPv6 0
  ✔ ok
```

`--check` runs without network access and fails (exit `1`) on unparsable lines,
duplicates and overlapping networks. Non-canonical and out-of-order entries are
reported as warnings only, because those are style issues rather than broken
data.

## Output guarantees

For `plain` (and the line-based formats):

- UTF-8, LF line endings, one subnet per line.
- No comments by default; `--with-header` adds `#`-prefixed metadata lines.
- Valid CIDR notation in canonical form (`2.56.60.0/22`, not
  `2.56.60.5/22` or `2.56.60.0/255.255.252.0`).
- Deduplicated, merged and sorted: IPv4 blocks first, then IPv6 blocks, each
  ordered by network address and prefix length.
- File mode `0644`, written atomically.

Because the list is already aggregated, it is safe to consume directly as a set
of networks; no further merging is required on the consumer side.

## How it works

1. **Validate** the country codes locally against the ISO 3166-1 alpha-2 list.
2. **Download** the zone file for each requested address family from the first
   source that responds successfully (optionally through the HTTP cache).
3. **Parse** each line as a CIDR network. Comment lines (`#`) and anything that
   does not parse as a network of the requested address family are skipped —
   some providers occasionally include headers or hostnames.
4. **Filter** by prefix bounds and subtract exclusions.
5. **Collapse** the networks with `ipaddress.collapse_addresses()`, which
   removes duplicates and merges adjacent or overlapping blocks into the
   smallest equivalent set, then sort the result.
6. **Render** into the requested format and **write** it to a temporary file
   next to the target, then `os.replace()` it into place.

## Data sources

| Source | URL pattern | Notes |
| --- | --- | --- |
| `ipverse` (default) | `https://raw.githubusercontent.com/ipverse/rir-ip/master/country/<cc>/ipv4-aggregated.txt` (and `ipv6-aggregated.txt`) | Derived from RIR delegation files, already aggregated per country. Preferred because it reflects registry delegations and serves files with `Content-Length`. |
| `ipdeny` (fallback) | `https://www.ipdeny.com/ipblocks/data/aggregated/<cc>-aggregated.zone` and `https://www.ipdeny.com/ipv6/ipaddresses/aggregated/<cc>-aggregated.zone` | Community-maintained aggregated zone files, widely used by firewall scripts. |

Both sources are third-party services. The two lists are close but not always
identical (a few hundred subnets in either direction for large countries),
because they are generated from different snapshots and aggregation rules. If
you need reproducible results, pin the source with `--source` and keep your own
copy of the generated file.

### Why the country code is validated locally

`ipdeny.com` answers a request for an unknown country code (for example `zz`)
with **HTTP 200 and the complete IPv4 table** instead of an error. Without local
validation the tool would happily write a "country" file containing the whole
internet. The ISO check prevents that class of silent, very hard to notice bug.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | No source could deliver data, everything was filtered out, the output could not be written, or `--check` found problems. |
| `2` | Invalid command line arguments. |
| `3` | `--if-changed --exit-code-changed` and the file was already current. |
| `130` | Interrupted with `Ctrl+C`. |

Errors are printed to stderr with an `error:` prefix, so `--quiet` runs remain
safe in pipelines.

## Environment variables

| Variable | Effect |
| --- | --- |
| `NO_COLOR` | Any non-empty value disables ANSI colors. |
| `TERM` | `TERM=dumb` disables ANSI colors. |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | Honored by `urllib`, so the tool works behind a corporate proxy. |

## Automation

### cron

```cron
# Refresh the Turkish list at 04:17 and reload the firewall only on change
17 4 * * * cd /srv/geoip && geo-download tr --quiet --if-changed && /usr/sbin/nft -f /srv/geoip/apply.nft
```

The file is only replaced after a fully successful download, so a network
failure leaves the previous list untouched and the job can simply be retried.

### systemd timer

```ini
# /etc/systemd/system/geo-tr.service
[Unit]
Description=Refresh Turkish IP block list

[Service]
Type=oneshot
WorkingDirectory=/srv/geoip
ExecStart=/usr/local/bin/geo-download tr --quiet --if-changed
ExecStartPost=/usr/sbin/nft -f /srv/geoip/apply.nft
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
geo-download tr --format ipset --set-name tr4 --stdout | ipset restore -exist
```

For IPv6, use `--family 6`, `family inet6` and a separate set name.

### nftables

```bash
nft add set inet filter tr4 '{ type ipv4_addr; flags interval; }'
nft add element inet filter tr4 "$(geo-download tr --format nft --stdout | tr -d '\n')"
```

In a ruleset file, paste the generated block after `elements =`.

### iptables

```bash
while read -r network; do
  iptables -A INPUT -s "$network" -j DROP
done < tr.txt
```

Slow for thousands of entries — prefer `ipset` or `nftables` sets, which use
hash lookups instead of a linear rule chain.

### nginx

```bash
geo-download tr --format nginx -o /etc/nginx/snippets/tr.allow
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
- **IPv4 by default.** IPv6 requires `--family 6` or `--family both`.
- **No caching unless asked.** Without `--cache-dir`, every invocation downloads
  fresh data; for a large fleet, run the tool once and distribute the file.
- **The progress bar starts when the response body arrives.** DNS resolution,
  TCP connect and the TLS handshake are not reflected in the bar. `--timeout`
  covers each socket operation, not the total wall clock time.
- **32 MiB hard limit** per file, as a guard against a misbehaving server
  returning an unbounded stream. No country comes close to this.
- **`--diff` compares network sets**, not file text, so reordering or a format
  change alone does not show up as a difference. Use `--check` for style
  issues.

## Troubleshooting

**`could not fetch IPv4 blocks for ...`** — every configured source failed. The
message contains the per-source reason. Check connectivity, proxy settings and
whether the source is temporarily unavailable; then retry or pin the other
source with `--source`.

**`unknown country code`** — the code is not in the ISO 3166-1 alpha-2 list.
Note that some codes exist in RIR data but not in ISO (`EU`, `AP`, `ZZ`), and
those are rejected by design.

**`no networks left for ...`** — exclusions or prefix bounds removed everything.
Check `--min-prefix`/`--max-prefix` and the exclusion file.

**No progress bar** — stderr is not a TTY (piped, redirected, captured by a
process manager), `--no-progress` was passed, or `--quiet` is active.

**Numbers differ from another tool** — aggregation rules and snapshot dates
differ between providers. Compare the collapsed sets, not the line counts:
1623 and 1626 subnets can describe exactly the same address space.

## Development

The project is a single module plus a standard-library test suite; no network
access is required to run the tests (they use a local HTTP server).

```bash
python3 -m unittest discover -s tests -v
ruff check .                            # lint (config pinned in pyproject.toml)
```

Manual sanity checks against the real sources:

```bash
python3 -m py_compile geo_download.py
python3 -m geo_download --version
python3 geo_download.py tr --quiet -o /tmp/tr.txt
python3 geo_download.py --check /tmp/tr.txt
```

Validate an existing list independently (no overlaps, correct ordering):

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
and remains subject to their respective terms. Generated lists contain only
registry delegation data (RIR allocations), not code, so they are not derived
works of this program; redistribute them under whatever terms you need.

### Contributing

Contributions are accepted under the same license. By submitting a change you
agree that it may be distributed under GPL-3.0-or-later. Please keep the
existing style: standard library only, ASCII-only source code, English
identifiers/comments/messages, tests for behaviour changes, and no new runtime
dependencies without a clear justification.
