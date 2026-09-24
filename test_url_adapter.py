"""Offline tests for url_adapter v3. Run: python3 test_url_adapter.py"""

import subprocess
import time
import unittest
from unittest.mock import Mock

import url_adapter as ua


def _ok(extra=""):
    m = Mock()
    m.returncode = 0
    m.stdout = extra
    m.stderr = ""
    return m


class TestResolve(unittest.TestCase):
    def test_bare_domain_gets_https(self):
        k, t = ua.resolve_url("example.com")
        self.assertEqual(k, "target")
        self.assertTrue(t["url"].startswith("https://"))

    def test_explicit_http_preserved(self):
        k, t = ua.resolve_url("http://example.com/")
        self.assertEqual(k, "target")
        self.assertTrue(t["url"].startswith("http://"))

    def test_reject_non_http(self):
        for s in ("javascript:alert(1)", "file:///etc/passwd",
                  "ftp://x.com/f", "about:blank", "data:text/plain,x"):
            k, _ = ua.resolve_url(s)
            self.assertEqual(k, "none", s)

    def test_reject_spaces(self):
        for s in ("not a url", "exam ple.com", "https://exam ple.com/"):
            k, _ = ua.resolve_url(s)
            self.assertEqual(k, "none", s)

    def test_empty(self):
        k, _ = ua.resolve_url("   ")
        self.assertEqual(k, "none")

    def test_bad_port_returns_none(self):
        k, _ = ua.resolve_url("https://example.com:99999/")
        self.assertEqual(k, "none")

    def test_ipv6_preserved(self):
        k, t = ua.resolve_url("https://[::1]/")
        self.assertEqual(k, "target")
        self.assertIn("[::1]", t["url"])

    def test_userinfo_rejected(self):
        k, _ = ua.resolve_url("https://user:pass@example.com/")
        self.assertEqual(k, "none")


class TestCompare(unittest.TestCase):
    def test_host_case_insensitive(self):
        ok, _ = ua.urls_equal("https://Example.COM/", "https://example.com/")
        self.assertTrue(ok)

    def test_empty_path_equals_slash(self):
        ok, _ = ua.urls_equal("https://example.com", "https://example.com/")
        self.assertTrue(ok)

    def test_http_upgrades_to_https(self):
        ok, _ = ua.urls_equal("http://example.com/", "https://example.com/")
        self.assertTrue(ok)

    def test_https_downgrade_not_equal(self):
        ok, _ = ua.urls_equal("https://example.com/", "http://example.com/")
        self.assertFalse(ok)

    def test_www_only_for_canonical_hosts(self):
        ok, _ = ua.urls_equal("https://www.youtube.com/", "https://youtube.com/")
        self.assertTrue(ok)
        ok, _ = ua.urls_equal("https://google.com/", "https://www.google.com/")
        self.assertTrue(ok)

    def test_www_not_equal_for_other_hosts(self):
        ok, _ = ua.urls_equal("https://example.com/", "https://www.example.com/")
        self.assertFalse(ok)
        ok, _ = ua.urls_equal("https://example.test/", "https://www.example.test/")
        self.assertFalse(ok)

    def test_youtube_www_redirect_now_matches(self):
        ok, _ = ua.urls_equal("https://youtube.com/", "https://www.youtube.com/")
        self.assertTrue(ok)

    def test_other_subdomain_not_equal(self):
        ok, _ = ua.urls_equal("https://youtube.com/", "https://m.youtube.com/")
        self.assertFalse(ok)

    def test_similar_public_suffix_not_equal(self):
        ok, _ = ua.urls_equal("https://youtube.com/", "https://youtube.co.uk/")
        self.assertFalse(ok)

    def test_explicit_default_port_not_equal_to_omitted(self):
        ok, _ = ua.urls_equal("https://example.com/", "https://example.com:443/")
        self.assertFalse(ok)

    def test_same_explicit_port_equal(self):
        ok, _ = ua.urls_equal("https://example.com:8443/", "https://example.com:8443/")
        self.assertTrue(ok)

    def test_query_order_matters(self):
        ok, _ = ua.urls_equal("https://e.com/?a=1&b=2", "https://e.com/?b=2&a=1")
        self.assertFalse(ok)

    def test_percent_vs_plus_not_equal(self):
        ok, _ = ua.urls_equal("https://e.com/?x=%20", "https://e.com/?x=+")
        self.assertFalse(ok)

    def test_fragment_matters(self):
        ok, _ = ua.urls_equal("https://e.com/#a", "https://e.com/#b")
        self.assertFalse(ok)

    def test_path_prefix_not_equal(self):
        ok, _ = ua.urls_equal("https://e.com/account", "https://e.com/account-delete")
        self.assertFalse(ok)


class TestSiteForName(unittest.TestCase):
    def test_known_sites(self):
        self.assertEqual(ua.site_for_name("YouTube"), "https://www.youtube.com/")
        self.assertEqual(ua.site_for_name("  GOOGLE "), "https://www.google.com/")
        self.assertEqual(ua.site_for_name("gmail"), "https://mail.google.com/")
        self.assertEqual(ua.site_for_name("GitHub"), "https://github.com/")
        self.assertEqual(ua.site_for_name("X"), "https://x.com/")
        self.assertEqual(ua.site_for_name("twitter"), "https://x.com/")
        self.assertEqual(ua.site_for_name("ChatGPT"), "https://chatgpt.com/")
        self.assertEqual(ua.site_for_name("Claude"), "https://claude.ai/")

    def test_unknown_names_none(self):
        for s in ("", "  ", "my bank", "youtube.com", "you tube",
                  "example", "http://github.com"):
            self.assertIsNone(ua.site_for_name(s), s)


class TestRunVerify(unittest.TestCase):
    def test_chrome_open_records_tab_id(self):
        run = Mock(return_value=_ok("ABC123\n"))
        t = {"url": "https://example.com/"}
        ua.run_url_open(t, time.time() + 5, _run=run,
                        _default_browser_fn=lambda to: "com.google.Chrome")
        args = run.call_args[0][0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("-e", args)
        self.assertEqual(args[-1], "https://example.com/")
        body = " ".join(args[1:args.index("--")])
        self.assertNotIn("example.com", body)
        self.assertIn("id of newTab", body)  # unique id, not wid:tidx
        self.assertEqual(t["tab"], "com.google.Chrome#ABC123")

    def test_chrome_verify_done_by_id(self):
        run = Mock(return_value=_ok("OK:https://example.com/\n"))
        t = {"url": "https://example.com/",
             "opened_with": "com.google.Chrome",
             "tab": "com.google.Chrome#ABC123"}
        st, _ = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "done")
        # Lookup keyed by tab id, not recycled index.
        sent = run.call_args[0][0]
        self.assertIn("ABC123", " ".join(sent))

    def test_chrome_recycled_id_closed_is_unverified(self):
        run = Mock(return_value=_ok("GONE:\n"))
        t = {"url": "https://example.com/",
             "opened_with": "com.google.Chrome",
             "tab": "com.google.Chrome#ABC123"}
        st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "unverified")
        self.assertIn("observed", facts)

    def test_chrome_navigated_away_unverified(self):
        run = Mock(return_value=_ok("OK:https://example.com/other\n"))
        t = {"url": "https://example.com/",
             "opened_with": "com.google.Chrome",
             "tab": "com.google.Chrome#ABC123"}
        st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "unverified")

    def test_chrome_newtab_waits_not_unverified(self):
        for observed in ("OK:chrome://newtab/\n", "OK:about:blank\n"):
            run = Mock(return_value=_ok(observed))
            t = {"url": "https://www.google.com/",
                 "opened_with": "com.google.Chrome",
                 "tab": "com.google.Chrome#ABC123"}
            st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
            self.assertEqual(st, "wait", observed)
            self.assertIn("not loaded", facts["reason"])
            self.assertEqual(facts["requested"], "https://www.google.com/")

    def test_chrome_strange_url_unverified_not_wait(self):
        for observed in ("OK:chrome://settings/\n", "OK:not a url at all\n",
                         "OK:ftp://example.test/f\n"):
            run = Mock(return_value=_ok(observed))
            t = {"url": "https://www.google.com/",
                 "opened_with": "com.google.Chrome",
                 "tab": "com.google.Chrome#ABC123"}
            st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
            self.assertEqual(st, "unverified", observed)
            self.assertEqual(facts["requested"], "https://www.google.com/")
            self.assertIn("observed", facts)

    def test_chrome_www_redirect_now_done(self):
        run = Mock(return_value=_ok("OK:https://www.youtube.com/\n"))
        t = {"url": "https://youtube.com/",
             "opened_with": "com.google.Chrome",
             "tab": "com.google.Chrome#ABC123"}
        st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "done")

    def test_safari_script_targets_new_tab(self):
        body = "\n".join(ua.SAFARI_OPEN)
        self.assertIn("set newTab to make new tab", body)
        self.assertIn("set URL of newTab", body)
        self.assertNotIn("set URL of front window", body)

    def test_safari_open_dispatches_but_verify_unverified(self):
        run = Mock(return_value=_ok("\n"))
        t = {"url": "https://example.com/"}
        ua.run_url_open(t, time.time() + 5, _run=run,
                        _default_browser_fn=lambda to: "com.apple.Safari")
        self.assertEqual(run.call_count, 1)  # dispatch happened
        self.assertIsNone(t["tab"])
        st, facts = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "unverified")
        self.assertEqual(run.call_count, 1)  # no index-based readback
        self.assertIn("no tab id", facts["reason"])

    def test_firefox_dispatches_open_b(self):
        run = Mock(return_value=_ok(""))
        t = {"url": "https://example.com/"}
        ua.run_url_open(t, time.time() + 5, _run=run,
                        _default_browser_fn=lambda to: "org.mozilla.firefox")
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["open", "-b", "org.mozilla.firefox"])
        self.assertEqual(args[3], "https://example.com/")
        self.assertEqual(t["opened_with"], "org.mozilla.firefox")
        st, _ = ua.verify_url_open(t, time.time() + 5, _run=run)
        self.assertEqual(st, "unverified")

    def test_lookup_failure_raises_no_launch(self):
        run = Mock()
        def _boom(to):
            raise RuntimeError("browser lookup failed: no default handler")
        with self.assertRaises(RuntimeError):
            ua.run_url_open({"url": "https://example.com/"},
                            time.time() + 5, _run=run,
                            _default_browser_fn=_boom)
        run.assert_not_called()

    def test_lookup_timeout_propagates(self):
        run = Mock()
        def _slow(to):
            raise TimeoutError("browser lookup timed out")
        with self.assertRaises(TimeoutError):
            ua.run_url_open({"url": "https://example.com/"},
                            time.time() + 5, _run=run,
                            _default_browser_fn=_slow)
        run.assert_not_called()

    def test_run_deadline_zero(self):
        with self.assertRaises(TimeoutError):
            ua.run_url_open({"url": "https://example.com/"},
                            time.time() - 1, _run=Mock())

    def test_smoke_shape(self):
        seen = {}
        def _fake(cmd, **kw):
            seen["cmd"] = cmd
            m = Mock()
            m.returncode = 0
            m.stdout = "ping\n"
            m.stderr = ""
            return m
        orig = ua._osascript_argv
        try:
            ua._osascript_argv = lambda lines, args, to, _run=None: _fake(
                ["osascript"] + [x for ln in lines for x in ("-e", ln)]
                + ["--"] + list(args))
            self.assertTrue(ua.osascript_smoke())
        finally:
            ua._osascript_argv = orig
        self.assertIn("-e", seen["cmd"])

    def test_real_lookup_returns_bundle_id(self):
        import sys
        exe = sys.executable
        try:
            import Cocoa  # noqa: F401
        except ImportError:
            exe = "/usr/bin/python3"
        bid = ua.default_browser_for_url("https://example.com/", 10,
                                         _exe=exe)
        self.assertTrue(isinstance(bid, str) and "." in bid)

    def test_lookup_uses_runner_with_timeout(self):
        seen = {}
        def _fake(cmd, **kw):
            seen["cmd"] = cmd
            seen["timeout"] = kw.get("timeout")
            m = Mock()
            m.returncode = 0
            m.stdout = "com.apple.Safari\n"
            m.stderr = ""
            return m
        bid = ua.default_browser_for_url("https://example.com/", 7,
                                         _run=_fake)
        self.assertEqual(bid, "com.apple.Safari")
        import sys
        self.assertEqual(seen["cmd"][0], sys.executable)
        self.assertEqual(seen["timeout"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
