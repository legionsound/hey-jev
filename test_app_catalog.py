"""Tests for app_catalog. Deterministic; stdlib unittest runnable."""
import os
import plistlib
import shutil
import tempfile
import unittest

import app_catalog as ac


def _make_app(root, relpath, bundle_id, display=None):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.join(path, "Contents"), exist_ok=True)
    info = {"CFBundleIdentifier": bundle_id}
    if display is not None:
        info["CFBundleDisplayName"] = display
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    return path


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="appcat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(ac._set_inventory, [])
        ac._set_inventory([])

    def _scan(self, *roots):
        return ac._scan(list(roots) if roots else [])

    def test_empty_roots_no_synthetic(self):
        self.assertEqual(self._scan(), [])
        ac._set_inventory(self._scan())
        self.assertEqual(ac.resolve_app("slack")[0], "none")

    def test_nested_apps_found_bundles_not_descended(self):
        _make_app(self.tmp, "Sub/Terminal.app", "com.apple.Terminal",
                  "Terminal")
        _make_app(self.tmp, "Sub/Nested/Deep.app", "com.ex.deep", "Deep")
        apps = self._scan(self.tmp)
        names = sorted(a["name"] for a in apps)
        self.assertEqual(names, ["Deep", "Terminal"])

    def test_bundle_without_id_skipped(self):
        bad = os.path.join(self.tmp, "NoId.app", "Contents")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleName": "NoId"}, f)
        self.assertEqual(self._scan(self.tmp), [])

    def test_canonical_dedup_distinct_installs_kept(self):
        p1 = _make_app(self.tmp, "A/Foo.app", "com.ex.foo", "Foo")
        p2 = _make_app(self.tmp, "B/Foo.app", "com.ex.foo", "Foo")
        inv = [{"name": "Foo", "bundle_id": "com.ex.foo", "path": p1},
               {"name": "Foo", "bundle_id": "com.ex.foo", "path": p2}]
        ac._set_inventory(inv)
        st, payload = ac.resolve_app("Foo")
        self.assertEqual(st, "choices")
        self.assertEqual(len(payload), 2)

    def test_alias_resolves_against_discovery_only(self):
        p = _make_app(self.tmp, "Chrome.app", "com.google.chrome",
                      "Google Chrome")
        ac._set_inventory([{"name": "Google Chrome",
                            "bundle_id": "com.google.chrome", "path": p}])
        st, payload = ac.resolve_app("chrome")
        self.assertEqual(st, "target")
        self.assertEqual(payload["bundle_id"], "com.google.chrome")
        ac._set_inventory([])
        self.assertEqual(ac.resolve_app("chrome")[0], "none")

    def test_substring_and_unicode_rejected(self):
        p = _make_app(self.tmp, "Safari.app", "com.apple.Safari", "Safari")
        ac._set_inventory([{"name": "Safari",
                            "bundle_id": "com.apple.Safari", "path": p}])
        self.assertEqual(ac.resolve_app("ari")[0], "none")
        self.assertEqual(ac.resolve_app("saf")[0], "none")
        self.assertEqual(ac.resolve_app("中文Safari")[0], "none")
        st, payload = ac.resolve_app("Safari")
        self.assertEqual(st, "target")
        self.assertEqual(payload["bundle_id"], "com.apple.Safari")

    def test_stem_match_and_empty(self):
        p = os.path.join(self.tmp, "MyTool.app")
        os.makedirs(os.path.join(p, "Contents"), exist_ok=True)
        with open(os.path.join(p, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.ex.tool",
                           "CFBundleName": "Something Else"}, f)
        ac._set_inventory([{"name": "Something Else",
                            "bundle_id": "com.ex.tool", "path": p}])
        self.assertEqual(ac.resolve_app("MyTool")[0], "target")
        self.assertEqual(ac.resolve_app("")[1], "empty app name")
        self.assertEqual(ac.resolve_app("zz-no-such-app-xyz")[0], "none")

    def test_whole_token_anywhere_unique_vs_choices(self):
        r1 = os.path.join(self.tmp, "DaVinci Resolve.app")
        r2 = os.path.join(self.tmp, "Uninstall Resolve.app")
        ac._set_inventory([
            {"name": "DaVinci Resolve", "bundle_id": "com.blackmagic.resolve",
             "path": r1},
            {"name": "Uninstall Resolve", "bundle_id": "com.blackmagic.uninst",
             "path": r2},
        ])
        st, payload = ac.resolve_app("resolve")
        self.assertEqual(st, "choices")
        self.assertEqual(len(payload), 2)
        ac._set_inventory([{"name": "DaVinci Resolve",
                            "bundle_id": "com.blackmagic.resolve", "path": r1}])
        st, payload = ac.resolve_app("resolve")
        self.assertEqual(st, "target")
        self.assertEqual(payload["bundle_id"], "com.blackmagic.resolve")

    def test_malformed_bundles_skipped_healthy_kept(self):
        good = _make_app(self.tmp, "Good.app", "com.ex.good", "Good")
        # list-root plist
        bad = os.path.join(self.tmp, "ListRoot.app", "Contents")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "Info.plist"), "wb") as f:
            plistlib.dump(["not", "a", "dict"], f)
        # non-string id + non-string name
        weird = os.path.join(self.tmp, "Weird.app", "Contents")
        os.makedirs(weird, exist_ok=True)
        with open(os.path.join(weird, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": 123,
                           "CFBundleDisplayName": ["x"]}, f)
        apps = self._scan(self.tmp)
        self.assertEqual([a["name"] for a in apps], ["Good"])
        self.assertEqual(apps[0]["path"], os.path.realpath(good))

    def test_app_inside_app_pruned(self):
        _make_app(self.tmp, "Outer.app", "com.ex.outer", "Outer")
        apps = self._scan(self.tmp)
        # helper .app nested inside Outer.app must not appear
        os.makedirs(os.path.join(self.tmp, "Outer.app", "Contents",
                                 "Helpers", "Inner.app", "Contents"),
                    exist_ok=True)
        with open(os.path.join(self.tmp, "Outer.app", "Contents", "Helpers",
                               "Inner.app", "Contents", "Info.plist"),
                  "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.ex.inner",
                           "CFBundleDisplayName": "Inner"}, f)
        apps = self._scan(self.tmp)
        self.assertEqual([a["name"] for a in apps], ["Outer"])

    def test_overlapping_roots_canonical_dedup(self):
        p = _make_app(self.tmp, "Sub/Foo.app", "com.ex.foo", "Foo")
        apps = ac._scan([self.tmp, os.path.join(self.tmp, "Sub"),
                         os.path.join(self.tmp, "Sub") + "/"])
        paths = [a["path"] for a in apps if a["name"] == "Foo"]
        self.assertEqual(paths, [os.path.realpath(p)])

    def test_two_chrome_installs_via_alias(self):
        p1 = _make_app(self.tmp, "A/Google Chrome.app",
                       "com.google.chrome", "Google Chrome")
        p2 = _make_app(self.tmp, "B/Google Chrome.app",
                       "com.google.chrome", "Google Chrome")
        ac._set_inventory([
            {"name": "Google Chrome", "bundle_id": "com.google.chrome",
             "path": p1},
            {"name": "Google Chrome", "bundle_id": "com.google.chrome",
             "path": p2}])
        st, payload = ac.resolve_app("chrome")
        self.assertEqual(st, "choices")
        self.assertEqual(len(payload), 2)

    def test_running_and_spotlight_dedup_by_realpath(self):
        p = _make_app(self.tmp, "Sub/Foo.app", "com.ex.foo", "Foo")
        apps = ac._scan([], _running=[p], _spotlight=[p], _folders=[])
        self.assertEqual([a["path"] for a in apps
                          if a["name"] == "Foo"],
                         [os.path.realpath(p)])
        self.assertEqual(ac.last_source_misses(), {})

    def test_spotlight_timeout_is_miss_not_error(self):
        p = _make_app(self.tmp, "Sub/Foo.app", "com.ex.foo", "Foo")
        apps = ac._scan([self.tmp], _running=[], _spotlight=None,
                        _folders=[])
        # explicit roots: extra sources skipped, no misses recorded
        self.assertTrue(any(a["name"] == "Foo" for a in apps))
        apps = ac._scan([], _running=[], _spotlight=None, _folders=[])
        self.assertEqual(apps, [])
        self.assertEqual(ac.last_source_misses(), {"mdfind": "unavailable"})

    def test_user_folder_symlink_and_missing(self):
        _make_app(self.tmp, "Extra/Bar.app", "com.ex.bar", "Bar")
        link = os.path.join(self.tmp, "LinkDir")
        try:
            os.symlink(os.path.join(self.tmp, "Extra"), link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        apps = ac._scan([], _running=[], _spotlight=[],
                        _folders=[link,
                                  os.path.join(self.tmp, "Nope")])
        self.assertEqual([a["name"] for a in apps], ["Bar"])
        self.assertEqual(apps[0]["path"],
                         os.path.realpath(
                             os.path.join(self.tmp, "Extra/Bar.app")))

    def test_app_folders_reads_shared_prefs_suite(self):
        import model_settings
        from unittest.mock import patch
        _make_app(self.tmp, "Extra/Bar.app", "com.ex.bar", "Bar")

        class FakePrefs:
            def arrayForKey_(self, key):
                assert key == "app_folders"
                return [self.folder]

        prefs = FakePrefs()
        prefs.folder = os.path.join(self.tmp, "Extra")
        with patch.object(model_settings, "PREFS", prefs):
            self.assertEqual(ac._app_folders(), [prefs.folder])
            apps = ac._scan([], _running=[], _spotlight=[],
                            _folders="auto")
            self.assertEqual([a["name"] for a in apps], ["Bar"])

    def test_refresh_returns_count(self):
        p = _make_app(self.tmp, "Sub/Foo.app", "com.ex.foo", "Foo")
        ac._set_inventory([])
        n = ac.refresh()
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)
        ac._set_inventory([{"name": "Foo", "bundle_id": "com.ex.foo",
                            "path": p}])
        self.assertEqual(ac.resolve_app("Foo")[0], "target")


if __name__ == "__main__":
    unittest.main()
