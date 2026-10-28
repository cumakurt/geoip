"""Test suite for geo_download.

All tests are network-free: HTTP behaviour is exercised against a local
``http.server`` instance bound to the loopback interface.
"""

from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import geo_download


class _Handler(BaseHTTPRequestHandler):
    """Serves ``server.routes`` and records every request."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        server.requests.append((self.path, dict(self.headers)))
        route = server.routes.get(self.path)
        if route is None:
            self.send_error(404)
            return

        status, body, headers = route
        if callable(status):
            # Dynamic route used to simulate flapping servers.
            status, body, headers = status(server, self)

        if status == 304:
            self.send_response(304)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class ServerTestCase(unittest.TestCase):
    """Base class providing a local HTTP server."""

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._stop_server)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = self._tmpdir.name

        # Retries must not slow the suite down.
        original_delay = geo_download.RETRY_BASE_DELAY_SECONDS
        geo_download.RETRY_BASE_DELAY_SECONDS = 0.0
        self.addCleanup(setattr, geo_download, "RETRY_BASE_DELAY_SECONDS", original_delay)

        self.console = geo_download.Console(stream=io.StringIO(), quiet=True, show_progress=False)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def add_route(self, path: str, body: str | bytes, status: int = 200, **headers: str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.server.routes[path] = (status, body, headers)

    def patch_sources(self, mapping: dict) -> None:
        patcher = mock.patch.dict(geo_download.SOURCES, mapping, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """Run ``main`` capturing stdout/stderr and returning (code, out, err)."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = geo_download.main(argv)
            except SystemExit as error:  # argparse errors
                code = int(error.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class ParseNetworksTests(unittest.TestCase):
    def test_skips_comments_blanks_and_garbage(self) -> None:
        text = "# comment\n\n2.0.0.0/8\nnot-a-network\n1.0.0.0/255.0.0.0\n3.0.0.0/8\n"
        networks = geo_download.parse_networks(text, 4)
        self.assertEqual([str(n) for n in networks], ["2.0.0.0/8", "1.0.0.0/8", "3.0.0.0/8"])

    def test_filters_by_family(self) -> None:
        text = "2.0.0.0/8\n2001:db8::/32\n"
        self.assertEqual(len(geo_download.parse_networks(text, 4)), 1)
        self.assertEqual(len(geo_download.parse_networks(text, 6)), 1)

    def test_strips_host_bits(self) -> None:
        networks = geo_download.parse_networks("2.56.60.5/22\n", 4)
        self.assertEqual(str(networks[0]), "2.56.60.0/22")

    def test_empty_input(self) -> None:
        self.assertEqual(geo_download.parse_networks("", 4), [])
        self.assertEqual(geo_download.parse_networks("# only comments\n", 4), [])


class CollapseTests(unittest.TestCase):
    def test_merges_adjacent_blocks(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("10.0.1.0/24")]
        self.assertEqual([str(n) for n in geo_download.collapse_networks(networks)], ["10.0.0.0/23"])

    def test_removes_duplicates(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/8")] * 3
        self.assertEqual([str(n) for n in geo_download.collapse_networks(networks)], ["10.0.0.0/8"])

    def test_handles_mixed_families(self) -> None:
        networks = [
            ipaddress.ip_network("2001:db8::/32"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("10.0.0.0/9"),
        ]
        collapsed = geo_download.collapse_networks(networks)
        self.assertEqual([str(n) for n in collapsed], ["10.0.0.0/8", "2001:db8::/32"])

    def test_result_is_sorted(self) -> None:
        networks = [
            ipaddress.ip_network("10.4.0.0/16"),
            ipaddress.ip_network("10.0.0.0/16"),
            ipaddress.ip_network("10.2.0.0/16"),
        ]
        collapsed = geo_download.collapse_networks(networks)
        self.assertEqual([str(n) for n in collapsed], ["10.0.0.0/16", "10.2.0.0/16", "10.4.0.0/16"])

    def test_empty(self) -> None:
        self.assertEqual(geo_download.collapse_networks([]), [])


class ExclusionTests(unittest.TestCase):
    def test_subtracts_inner_network(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/8")]
        exclusions = [ipaddress.ip_network("10.1.0.0/16")]
        matched: set = set()
        result = geo_download.apply_exclusions(networks, exclusions, 4, matched)
        self.assertEqual(sum(n.num_addresses for n in result), 2**24 - 2**16)
        self.assertEqual(matched, set(exclusions))

    def test_removes_whole_network(self) -> None:
        networks = [ipaddress.ip_network("10.1.0.0/16")]
        exclusions = [ipaddress.ip_network("10.0.0.0/8")]
        result = geo_download.apply_exclusions(networks, exclusions, 4, set())
        self.assertEqual(result, [])

    def test_removes_exact_network(self) -> None:
        network = ipaddress.ip_network("10.1.0.0/16")
        result = geo_download.apply_exclusions([network], [network], 4, set())
        self.assertEqual(result, [])

    def test_ignores_disjoint_and_other_family(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/8")]
        exclusions = [
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("2001:db8::/32"),
        ]
        matched: set = set()
        result = geo_download.apply_exclusions(networks, exclusions, 4, matched)
        self.assertEqual([str(n) for n in result], ["10.0.0.0/8"])
        self.assertEqual(matched, set())

    def test_multiple_exclusions_are_ordered_independent(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/8")]
        exclusions = [
            ipaddress.ip_network("10.0.0.0/9"),
            ipaddress.ip_network("10.128.0.0/10"),
        ]
        result = geo_download.apply_exclusions(networks, exclusions, 4, set())
        self.assertEqual(sum(n.num_addresses for n in result), 2**24 - 2**23 - 2**22)

    def test_no_duplicate_or_overlapping_pieces(self) -> None:
        networks = [ipaddress.ip_network("10.0.0.0/8")]
        exclusions = [
            ipaddress.ip_network("10.0.0.0/24"),
            ipaddress.ip_network("10.0.0.128/25"),
        ]
        pieces = geo_download.apply_exclusions(networks, exclusions, 4, set())
        collapsed = geo_download.collapse_networks(pieces)
        total = sum(network.num_addresses for network in collapsed)
        # The /24 and the /25 inside it are removed, leaving the rest of the /8.
        self.assertEqual(total, 2**24 - 2**8)
        self.assertEqual(total, sum(network.num_addresses for network in pieces))


class PrefixFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = geo_download.Console(stream=io.StringIO(), quiet=True, show_progress=False)
        self.networks = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/28"),
        ]

    def test_no_bounds_keeps_everything(self) -> None:
        result = geo_download.apply_prefix_filters(self.networks, None, None, 4, self.console)
        self.assertEqual(result, self.networks)

    def test_min_prefix_drops_broad_networks(self) -> None:
        result = geo_download.apply_prefix_filters(self.networks, 24, None, 4, self.console)
        self.assertEqual([str(n) for n in result], ["192.0.2.0/24", "198.51.100.0/28"])

    def test_max_prefix_drops_narrow_networks(self) -> None:
        result = geo_download.apply_prefix_filters(self.networks, None, 24, 4, self.console)
        self.assertEqual([str(n) for n in result], ["10.0.0.0/8", "192.0.2.0/24"])

    def test_both_bounds(self) -> None:
        result = geo_download.apply_prefix_filters(self.networks, 8, 24, 4, self.console)
        self.assertEqual([str(n) for n in result], ["10.0.0.0/8", "192.0.2.0/24"])

    def test_warns_when_something_is_dropped(self) -> None:
        stream = io.StringIO()
        console = geo_download.Console(stream=stream, quiet=False, show_progress=False)
        geo_download.apply_prefix_filters(self.networks, 24, None, 4, console)
        self.assertIn("dropped 1 networks", stream.getvalue())


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.networks = geo_download.collapse_networks(
            [
                ipaddress.ip_network("2.56.60.0/22"),
                ipaddress.ip_network("2001:678:1a4::/48"),
            ]
        )

    def test_plain(self) -> None:
        lines = geo_download.render_networks(self.networks, "plain")
        self.assertEqual(lines, ["2.56.60.0/22", "2001:678:1a4::/48"])

    def test_ipset_uses_set_name(self) -> None:
        lines = geo_download.render_networks(self.networks, "ipset", set_name="tr4")
        self.assertEqual(lines, ["add tr4 2.56.60.0/22", "add tr4 2001:678:1a4::/48"])

    def test_nginx(self) -> None:
        lines = geo_download.render_networks(self.networks, "nginx")
        self.assertEqual(lines, ["allow 2.56.60.0/22;", "allow 2001:678:1a4::/48;"])

    def test_nft_block(self) -> None:
        lines = geo_download.render_networks(self.networks, "nft")
        self.assertEqual(lines[0], "{")
        self.assertEqual(lines[1], "    2.56.60.0/22,")
        self.assertEqual(lines[-1], "}")

    def test_nft_empty(self) -> None:
        self.assertEqual(geo_download.render_networks([], "nft"), ["{", "}"])

    def test_json_is_valid_and_complete(self) -> None:
        lines = geo_download.render_networks(self.networks, "json")
        payload = json.loads("\n".join(lines))
        self.assertEqual(payload["ipv4"], ["2.56.60.0/22"])
        self.assertEqual(payload["ipv6"], ["2001:678:1a4::/48"])
        self.assertEqual(payload["total_count"], 2)
        self.assertEqual(payload["address_count"], 2**10 + 2**80)

    def test_json_is_deterministic(self) -> None:
        first = geo_download.render_networks(self.networks, "json")
        second = geo_download.render_networks(self.networks, "json")
        self.assertEqual(first, second)

    def test_header_is_prepended_and_stable(self) -> None:
        header = ["# country: TR", "# total: 2 subnets"]
        lines = geo_download.render_networks(self.networks, "plain", header=header)
        self.assertEqual(lines[:2], header)
        self.assertEqual(lines[2], "2.56.60.0/22")

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            geo_download.render_networks(self.networks, "yaml")

    def test_build_header_contents(self) -> None:
        header = geo_download.build_header("TR", ["ipverse", "ipverse"], self.networks)
        text = "\n".join(header)
        self.assertIn("# country: TR", text)
        self.assertIn("# source: ipverse", text)
        self.assertIn("# ipv4: 1 subnets", text)
        self.assertIn("# ipv6: 1 subnets", text)
        self.assertIn("# total: 2 subnets", text)


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(geo_download.format_bytes(0), "0 B")
        self.assertEqual(geo_download.format_bytes(999), "999 B")
        self.assertEqual(geo_download.format_bytes(1024), "1.0 KB")
        self.assertEqual(geo_download.format_bytes(25_600), "25.0 KB")
        self.assertEqual(geo_download.format_bytes(16_826_048), "16.0 MB")

    def test_format_eta(self) -> None:
        self.assertEqual(geo_download.format_eta(0), "00:00")
        self.assertEqual(geo_download.format_eta(67), "01:07")

    def test_format_duration(self) -> None:
        self.assertEqual(geo_download.format_duration(0.014), "14 ms")
        self.assertEqual(geo_download.format_duration(1.24), "1.2 s")
        self.assertEqual(geo_download.format_duration(67), "1m 07s")


class ConsoleTests(unittest.TestCase):
    def test_quiet_suppresses_output(self) -> None:
        stream = io.StringIO()
        console = geo_download.Console(stream=stream, quiet=True, show_progress=True)
        console.line("hello")
        console.warn("careful")
        self.assertEqual(stream.getvalue(), "")

    def test_non_tty_disables_color_and_progress(self) -> None:
        stream = io.StringIO()
        console = geo_download.Console(stream=stream, quiet=False)
        self.assertFalse(console.uses_color)
        self.assertFalse(console.progress_enabled)
        self.assertFalse(console.uses_unicode)
        console.line(console.style("x", "ok"))
        self.assertEqual(stream.getvalue(), "x\n")

    def test_progress_with_known_total(self) -> None:
        console = geo_download.Console(stream=io.StringIO(), quiet=False, show_progress=True)
        console.progress_enabled = True
        console.uses_unicode = True
        console.uses_color = False
        progress = geo_download.DownloadProgress(console, "IPv4 - ipverse")
        progress.start(1000)
        progress.update(500)
        frame = progress._compose(geo_download.time.monotonic())
        self.assertIn("50%", frame)
        self.assertIn("/ 1000", frame.replace("1000 B", "1000"))
        self.assertIn("IPv4", frame)

    def test_progress_without_total_shows_spinner(self) -> None:
        console = geo_download.Console(stream=io.StringIO(), quiet=False, show_progress=True)
        console.progress_enabled = True
        console.uses_color = False
        progress = geo_download.DownloadProgress(console, "IPv4")
        progress.start(None)
        progress.update(1234)
        frame = progress._compose(geo_download.time.monotonic())
        self.assertNotIn("%", frame)
        self.assertIn("1.2 KB", frame)


# --------------------------------------------------------------------------- #
# HTTP behaviour
# --------------------------------------------------------------------------- #


class DownloadTests(ServerTestCase):
    def test_downloads_file(self) -> None:
        self.add_route("/zone.txt", "2.56.60.0/22\n2.56.152.0/22\n")
        body = geo_download.fetch_text(self.base_url + "/zone.txt", 5)
        self.assertIn("2.56.60.0/22", body)

    def test_sends_user_agent(self) -> None:
        self.add_route("/zone.txt", "2.0.0.0/8\n")
        geo_download.fetch_text(self.base_url + "/zone.txt", 5)
        _path, headers = self.server.requests[-1]
        self.assertIn("geo-download", headers["User-Agent"])

    def test_404_is_permanent_and_not_retried(self) -> None:
        with self.assertRaises(geo_download.DownloadError) as ctx:
            geo_download.fetch_text(self.base_url + "/missing.txt", 5)
        self.assertIn("404", str(ctx.exception))
        self.assertEqual(len(self.server.requests), 1)

    def test_retries_transient_error(self) -> None:
        state = {"count": 0}

        def flapping(server, handler):
            state["count"] += 1
            if state["count"] < 3:
                return 503, b"busy", {}
            return 200, b"2.0.0.0/8\n", {}

        self.server.routes["/flap.txt"] = (flapping, b"", {})
        body = geo_download.fetch_text(self.base_url + "/flap.txt", 5)
        self.assertIn("2.0.0.0/8", body)
        self.assertEqual(len(self.server.requests), 3)

    def test_gives_up_after_retry_budget(self) -> None:
        self.server.routes["/down.txt"] = (lambda server, handler: (503, b"", {}), b"", {})
        with self.assertRaises(geo_download.DownloadError):
            geo_download.fetch_text(self.base_url + "/down.txt", 5)
        self.assertEqual(len(self.server.requests), geo_download.RETRY_ATTEMPTS)

    def test_rejects_oversized_content_length(self) -> None:
        self.add_route("/big.txt", b"0" * 32, **{"Content-Length": str(geo_download.MAX_DOWNLOAD_BYTES + 1)})
        with self.assertRaises(geo_download.DownloadError) as ctx:
            geo_download.fetch_text(self.base_url + "/big.txt", 5)
        self.assertIn("larger than", str(ctx.exception))

    def test_progress_receives_bytes_and_totals(self) -> None:
        body = "2.0.0.0/8\n" * 100
        self.add_route("/zone.txt", body)
        console = geo_download.Console(stream=io.StringIO(), quiet=False, show_progress=True)
        console.progress_enabled = True
        console.uses_color = False
        progress = geo_download.DownloadProgress(console, "IPv4")
        geo_download.fetch_text(self.base_url + "/zone.txt", 5, progress)
        self.assertEqual(progress.downloaded, len(body))
        self.assertEqual(progress.total, len(body))
        self.assertFalse(progress.visible)


class CacheTests(ServerTestCase):
    def test_second_request_is_conditional(self) -> None:
        def route(server, handler):
            if handler.headers.get("If-None-Match") == '"v1"':
                return 304, b"", {"ETag": '"v1"'}
            return 200, b"2.0.0.0/8\n", {"ETag": '"v1"'}

        self.server.routes["/zone.txt"] = (route, b"", {})
        cache_dir = os.path.join(self.tmp, "cache")
        url = self.base_url + "/zone.txt"

        first = geo_download.fetch_text(url, 5, cache_dir=cache_dir)
        second = geo_download.fetch_text(url, 5, cache_dir=cache_dir)

        self.assertEqual(first, second)
        self.assertIn("If-None-Match", self.server.requests[-1][1])
        self.assertEqual(len(os.listdir(cache_dir)), 2)

    def test_cache_survives_corruption(self) -> None:
        self.add_route("/zone.txt", "2.0.0.0/8\n")
        cache_dir = os.path.join(self.tmp, "cache")
        url = self.base_url + "/zone.txt"
        geo_download.fetch_text(url, 5, cache_dir=cache_dir)

        body_path, meta_path = geo_download.cache_paths(cache_dir, url)
        with open(meta_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        body = geo_download.fetch_text(url, 5, cache_dir=cache_dir)
        self.assertIn("2.0.0.0/8", body)
        self.assertTrue(os.path.exists(body_path))

    def test_unwritable_cache_is_ignored(self) -> None:
        self.add_route("/zone.txt", "2.0.0.0/8\n")
        blocked = os.path.join(self.tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        body = geo_download.fetch_text(
            self.base_url + "/zone.txt", 5, cache_dir=os.path.join(blocked, "cache")
        )
        self.assertIn("2.0.0.0/8", body)


class FallbackTests(ServerTestCase):
    def test_falls_back_to_second_source(self) -> None:
        self.add_route("/ipdeny.txt", "2.0.0.0/8\n")
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/missing-{cc}.txt",
                ("ipdeny", 4): self.base_url + "/ipdeny.txt",
            }
        )
        networks, source, _progress = geo_download.download_country(
            "TR", 4, geo_download.parse_args(["tr"]), self.console
        )
        self.assertEqual(source, "ipdeny")
        self.assertEqual([str(n) for n in networks], ["2.0.0.0/8"])

    def test_reports_all_sources_failing(self) -> None:
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/a-{cc}.txt",
                ("ipdeny", 4): self.base_url + "/b-{cc}.txt",
            }
        )
        with self.assertRaises(geo_download.DownloadError) as ctx:
            geo_download.download_country("TR", 4, geo_download.parse_args(["tr"]), self.console)
        message = str(ctx.exception)
        self.assertIn("ipverse", message)
        self.assertIn("ipdeny", message)

    def test_empty_response_is_treated_as_failure(self) -> None:
        self.add_route("/empty.txt", "# nothing here\n")
        self.add_route("/ipdeny.txt", "2.0.0.0/8\n")
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/empty.txt",
                ("ipdeny", 4): self.base_url + "/ipdeny.txt",
            }
        )
        networks, source, _progress = geo_download.download_country(
            "TR", 4, geo_download.parse_args(["tr"]), self.console
        )
        self.assertEqual(source, "ipdeny")
        self.assertEqual(len(networks), 1)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = self._tmpdir.name

    def test_writes_lines_with_trailing_newline(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        geo_download.write_atomically(path, ["2.0.0.0/8", "3.0.0.0/8"])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2.0.0.0/8\n3.0.0.0/8\n")

    def test_creates_missing_directories(self) -> None:
        path = os.path.join(self.tmp, "a", "b", "out.txt")
        geo_download.write_atomically(path, ["2.0.0.0/8"])
        self.assertTrue(os.path.exists(path))

    def test_no_temporary_files_left_behind(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        geo_download.write_atomically(path, ["2.0.0.0/8"])
        self.assertEqual(os.listdir(self.tmp), ["out.txt"])

    def test_failure_keeps_previous_content(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        geo_download.write_atomically(path, ["2.0.0.0/8"])

        original_replace = os.replace

        def failing_replace(source, destination):
            raise OSError("disk full")

        os.replace = failing_replace
        try:
            with self.assertRaises(OSError):
                geo_download.write_atomically(path, ["3.0.0.0/8"])
        finally:
            os.replace = original_replace

        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2.0.0.0/8\n")
        self.assertEqual(os.listdir(self.tmp), ["out.txt"])

    def test_file_content_matches(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        geo_download.write_atomically(path, ["2.0.0.0/8"])
        self.assertTrue(geo_download.file_content_matches(path, ["2.0.0.0/8"]))
        self.assertFalse(geo_download.file_content_matches(path, ["3.0.0.0/8"]))
        self.assertFalse(geo_download.file_content_matches(os.path.join(self.tmp, "nope"), []))


# --------------------------------------------------------------------------- #
# Check mode
# --------------------------------------------------------------------------- #


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = self._tmpdir.name
        self.stream = io.StringIO()
        self.console = geo_download.Console(stream=self.stream, quiet=False, show_progress=False)

    def write(self, text: str) -> str:
        path = os.path.join(self.tmp, "list.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_valid_file_passes(self) -> None:
        path = self.write("2.0.0.0/8\n3.0.0.0/8\n")
        self.assertEqual(geo_download.check_file(path, self.console), 0)
        self.assertIn("ok", self.stream.getvalue())

    def test_invalid_line_fails(self) -> None:
        path = self.write("2.0.0.0/8\nbogus\n")
        self.assertEqual(geo_download.check_file(path, self.console), 1)
        self.assertIn("not a network", self.stream.getvalue())

    def test_duplicate_fails(self) -> None:
        path = self.write("2.0.0.0/8\n2.0.0.0/8\n")
        self.assertEqual(geo_download.check_file(path, self.console), 1)
        self.assertIn("duplicate", self.stream.getvalue())

    def test_overlap_fails(self) -> None:
        path = self.write("10.0.0.0/8\n10.1.0.0/16\n")
        self.assertEqual(geo_download.check_file(path, self.console), 1)
        self.assertIn("overlapping", self.stream.getvalue())

    def test_non_canonical_warns_only(self) -> None:
        path = self.write("2.56.60.5/22\n")
        self.assertEqual(geo_download.check_file(path, self.console), 0)
        self.assertIn("not canonical", self.stream.getvalue())

    def test_missing_file(self) -> None:
        self.assertEqual(geo_download.check_file(os.path.join(self.tmp, "nope.txt"), self.console), 1)

    def test_comments_and_blanks_are_ignored(self) -> None:
        path = self.write("# header\n\n2.0.0.0/8\n")
        self.assertEqual(geo_download.check_file(path, self.console), 0)

    def test_out_of_order_warns_only(self) -> None:
        path = self.write("3.0.0.0/8\n2.0.0.0/8\n")
        self.assertEqual(geo_download.check_file(path, self.console), 0)
        self.assertIn("out of order", self.stream.getvalue())


# --------------------------------------------------------------------------- #
# Command line integration
# --------------------------------------------------------------------------- #


class CliValidationTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = geo_download.main(argv)
            except SystemExit as error:
                code = int(error.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_requires_country(self) -> None:
        code, _out, err = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("country code is required", err)

    def test_rejects_unknown_country(self) -> None:
        code, _out, err = self.run_cli(["zz"])
        self.assertEqual(code, 2)
        self.assertIn("unknown country code", err)

    def test_rejects_malformed_country(self) -> None:
        code, _out, err = self.run_cli(["xyz"])
        self.assertEqual(code, 2)
        self.assertIn("invalid country code", err)

    def test_rejects_bad_timeout(self) -> None:
        code, _out, err = self.run_cli(["tr", "--timeout", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--timeout", err)

    def test_rejects_output_with_multiple_countries(self) -> None:
        code, _out, err = self.run_cli(["tr", "de", "-o", "x.txt"])
        self.assertEqual(code, 2)
        self.assertIn("single country", err)

    def test_rejects_stdout_with_multiple_countries(self) -> None:
        code, _out, err = self.run_cli(["tr", "de", "--stdout"])
        self.assertEqual(code, 2)
        self.assertIn("single country", err)

    def test_rejects_output_and_stdout_together(self) -> None:
        code, _out, err = self.run_cli(["tr", "-o", "x.txt", "--stdout"])
        self.assertEqual(code, 2)
        self.assertIn("not both", err)

    def test_rejects_set_name_without_ipset(self) -> None:
        code, _out, err = self.run_cli(["tr", "--set-name", "x"])
        self.assertEqual(code, 2)
        self.assertIn("--set-name", err)

    def test_rejects_header_with_json(self) -> None:
        code, _out, err = self.run_cli(["tr", "--format", "json", "--with-header"])
        self.assertEqual(code, 2)
        self.assertIn("--with-header", err)

    def test_rejects_if_changed_with_header(self) -> None:
        code, _out, err = self.run_cli(["tr", "--if-changed", "--with-header"])
        self.assertEqual(code, 2)
        self.assertIn("--if-changed", err)

    def test_rejects_if_changed_with_stdout(self) -> None:
        code, _out, err = self.run_cli(["tr", "--if-changed", "--stdout"])
        self.assertEqual(code, 2)
        self.assertIn("--if-changed", err)

    def test_rejects_exit_code_changed_alone(self) -> None:
        code, _out, err = self.run_cli(["tr", "--exit-code-changed"])
        self.assertEqual(code, 2)
        self.assertIn("--exit-code-changed", err)

    def test_rejects_invalid_exclusion(self) -> None:
        code, _out, err = self.run_cli(["tr", "--exclude", "not-a-network"])
        self.assertEqual(code, 2)
        self.assertIn("invalid network", err)

    def test_rejects_bad_prefix_bounds(self) -> None:
        code, _out, err = self.run_cli(["tr", "--min-prefix", "30", "--max-prefix", "8"])
        self.assertEqual(code, 2)
        self.assertIn("--min-prefix", err)

    def test_rejects_prefix_out_of_range(self) -> None:
        code, _out, err = self.run_cli(["tr", "--min-prefix", "200"])
        self.assertEqual(code, 2)
        self.assertIn("between 0 and 128", err)

    def test_rejects_ipv6_with_family(self) -> None:
        code, _out, err = self.run_cli(["tr", "--ipv6", "--family", "4"])
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", err)

    def test_rejects_mixed_family_for_set_formats(self) -> None:
        for output_format in ("ipset", "nft"):
            code, _out, err = self.run_cli(["tr", "--ipv6", "--format", output_format])
            self.assertEqual(code, 2, output_format)
            self.assertIn("single address family", err)

    def test_rejects_check_with_country(self) -> None:
        code, _out, err = self.run_cli(["tr", "--check", "x.txt"])
        self.assertEqual(code, 2)
        self.assertIn("--check", err)

    def test_version_exits_zero(self) -> None:
        code, out, _err = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(geo_download.__version__, out)

    def test_help_lists_new_options(self) -> None:
        code, out, _err = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        for option in (
            "--format",
            "--stdout",
            "--exclude",
            "--if-changed",
            "--diff",
            "--check",
            "--cache-dir",
            "--completion",
            "--min-prefix",
        ):
            self.assertIn(option, out)


class CliRunTests(ServerTestCase):
    """End-to-end runs against the local server."""

    def setUp(self) -> None:
        super().setUp()
        self.add_route("/v4.txt", "2.56.60.0/22\n2.56.152.0/22\n")
        self.add_route("/v6.txt", "2001:678:1a4::/48\n")
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/v4.txt",
                ("ipverse", 6): self.base_url + "/v6.txt",
            }
        )

    def test_plain_file_default_name(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, _out, _err = self.run_cli(["tr", "--quiet"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, "tr.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2.56.60.0/22\n2.56.152.0/22\n")

    def test_stdout_keeps_data_clean(self) -> None:
        code, out, err = self.run_cli(["tr", "--stdout"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "2.56.60.0/22\n2.56.152.0/22\n")
        self.assertNotIn("\r", out)
        self.assertIn("geo-download", err)

    def test_ipv6_flag_produces_both_families(self) -> None:
        code, out, _err = self.run_cli(["tr", "--ipv6", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertIn("2.56.60.0/22", out)
        self.assertIn("2001:678:1a4::/48", out)
        self.assertLess(out.index("2.56.60.0/22"), out.index("2001:678:1a4::/48"))

    def test_family_six_only(self) -> None:
        code, out, _err = self.run_cli(["tr", "--family", "6", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "2001:678:1a4::/48\n")

    def test_format_ipset_stdout(self) -> None:
        code, out, _err = self.run_cli(["tr", "--format", "ipset", "--set-name", "tr4", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "add tr4 2.56.60.0/22")

    def test_ipset_with_single_ipv6_family(self) -> None:
        code, out, _err = self.run_cli(["tr", "--family", "6", "--format", "ipset", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "add tr 2001:678:1a4::/48\n")

    def test_format_json_stdout(self) -> None:
        code, out, _err = self.run_cli(["tr", "--format", "json", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["total_count"], 2)

    def test_format_nft_stdout(self) -> None:
        code, out, _err = self.run_cli(["tr", "--format", "nft", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "{")
        self.assertEqual(lines[-1], "}")

    def test_exclude_removes_network(self) -> None:
        code, out, _err = self.run_cli(["tr", "--exclude", "2.56.60.0/22", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "2.56.152.0/22\n")

    def test_exclude_file(self) -> None:
        path = os.path.join(self.tmp, "exclude.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# remove this\n2.56.60.0/22\n")
        code, out, _err = self.run_cli(["tr", "--exclude-file", path, "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "2.56.152.0/22\n")

    def test_unmatched_exclusion_warns(self) -> None:
        code, _out, err = self.run_cli(["tr", "--exclude", "203.0.113.0/24", "--stdout"])
        self.assertEqual(code, 0)
        self.assertIn("matched no downloaded network", err)

    def test_min_prefix_filters(self) -> None:
        self.add_route("/mixed.txt", "2.56.60.0/22\n198.51.100.0/24\n")
        self.patch_sources({("ipverse", 4): self.base_url + "/mixed.txt"})
        code, out, _err = self.run_cli(["tr", "--min-prefix", "23", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "198.51.100.0/24\n")

    def test_with_header_writes_comments(self) -> None:
        code, out, _err = self.run_cli(["tr", "--with-header", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# generated by geo-download"))
        self.assertIn("# country: TR", out)

    def test_if_changed_skips_second_write(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        first, _out, _err = self.run_cli(["tr", "-o", path, "--quiet"])
        self.assertEqual(first, 0)
        inode = os.stat(path).st_ino

        second, _out, err = self.run_cli(["tr", "-o", path, "--if-changed"])
        self.assertEqual(second, 0)
        self.assertIn("unchanged", err)
        self.assertEqual(os.stat(path).st_ino, inode)

    def test_exit_code_changed_reports_unchanged(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        self.run_cli(["tr", "-o", path, "--quiet"])
        code, _out, _err = self.run_cli(["tr", "-o", path, "--quiet", "--if-changed", "--exit-code-changed"])
        self.assertEqual(code, geo_download.EXIT_UNCHANGED)

    def test_exit_code_changed_reports_change(self) -> None:
        path = os.path.join(self.tmp, "out.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("192.0.2.0/24\n")
        code, _out, _err = self.run_cli(["tr", "-o", path, "--quiet", "--if-changed", "--exit-code-changed"])
        self.assertEqual(code, 0)

    def test_diff_reports_changes(self) -> None:
        path = os.path.join(self.tmp, "old.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("192.0.2.0/24\n")
        code, _out, err = self.run_cli(["tr", "-o", os.path.join(self.tmp, "new.txt"), "--diff", path])
        self.assertEqual(code, 0)
        self.assertIn("+ 2.56.60.0/22", err)
        self.assertIn("- 192.0.2.0/24", err)
        self.assertIn("+2 -1", err)

    def test_diff_reports_no_changes(self) -> None:
        path = os.path.join(self.tmp, "same.txt")
        self.run_cli(["tr", "-o", path, "--quiet"])
        code, _out, err = self.run_cli(["tr", "-o", path, "--diff", path])
        self.assertEqual(code, 0)
        self.assertIn("diff: no changes", err)

    def test_multiple_countries_write_separate_files(self) -> None:
        self.add_route("/tr-v4.txt", "2.56.60.0/22\n")
        self.add_route("/de-v4.txt", "2.28.0.0/14\n")
        self.patch_sources({("ipverse", 4): self.base_url + "/{cc}-v4.txt"})
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, _out, _err = self.run_cli(["tr", "de", "--quiet"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, "tr.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2.56.60.0/22\n")
        with open(os.path.join(self.tmp, "de.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2.28.0.0/14\n")

    def test_download_failure_returns_error(self) -> None:
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/nowhere-{cc}.txt",
                ("ipdeny", 4): self.base_url + "/nowhere2-{cc}.txt",
            }
        )
        code, _out, err = self.run_cli(["tr", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_all_filtered_out_is_an_error(self) -> None:
        code, _out, err = self.run_cli(["tr", "--max-prefix", "8", "--stdout", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("no networks left", err)

    def test_empty_source_then_fallback(self) -> None:
        self.add_route("/empty.txt", "# nothing\n")
        self.add_route("/real.txt", "198.51.100.0/24\n")
        self.patch_sources(
            {
                ("ipverse", 4): self.base_url + "/missing-{cc}.txt",
                ("ipdeny", 4): self.base_url + "/real.txt",
            }
        )
        code, out, _err = self.run_cli(["tr", "--stdout", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "198.51.100.0/24\n")


class CompletionTests(unittest.TestCase):
    def test_bash_script_is_well_formed(self) -> None:
        script = geo_download.render_completion("bash")
        self.assertIn("complete -F _geo_download", script)
        self.assertIn("_geo_download_countries=", script)
        self.assertIn(" tr", script)
        self.assertEqual(script.count("_geo_download() {"), 1)
        self.assertEqual(script.count("}}"), 0)

    def test_zsh_script_is_well_formed(self) -> None:
        script = geo_download.render_completion("zsh")
        self.assertIn("#compdef", script)
        self.assertIn("_arguments", script)
        self.assertIn("compadd", script)

    def test_unknown_shell_raises(self) -> None:
        with self.assertRaises(ValueError):
            geo_download.render_completion("fish")

    def test_bash_syntax_check(self) -> None:
        script = geo_download.render_completion("bash")
        path = os.path.join(tempfile.mkdtemp(), "completion.bash")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        status = os.system(f"bash -n {path} 2>/dev/null")
        self.assertEqual(status, 0)

    def test_cli_prints_completion_and_exits(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = geo_download.main(["--completion", "bash"])
        self.assertEqual(code, 0)
        self.assertIn("complete -F", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
