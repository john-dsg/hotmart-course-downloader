import json
import unittest
from pathlib import Path

import browser_bridge


class BrowserBridgeTests(unittest.TestCase):
    def test_accepts_only_hotmart_https_manifests(self):
        self.assertTrue(
            browser_bridge.valid_manifest_url(
                "https://vod.play.hotmart.com/video/example/hls/master.m3u8?token=temporary"
            )
        )
        for invalid in (
            "http://vod.play.hotmart.com/video/example/hls/master.m3u8",
            "https://evil.example/video/example/hls/master.m3u8",
            "https://hotmart.com.example/video/example/hls/master.m3u8",
            "https://vod.play.hotmart.com/video/example/file.mp4",
        ):
            self.assertFalse(browser_bridge.valid_manifest_url(invalid))

    def test_extension_has_narrow_permissions(self):
        path = Path(__file__).parents[1] / "browser_bridge" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["permissions"], ["webRequest"])
        self.assertNotIn("cookies", manifest["permissions"])
        self.assertNotIn("downloads", manifest["permissions"])
        self.assertFalse(
            any("<all_urls>" in item for item in manifest["host_permissions"])
        )


if __name__ == "__main__":
    unittest.main()
