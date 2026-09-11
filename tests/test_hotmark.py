import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import m3u8

import hotmark


class SecurityTests(unittest.TestCase):
    def test_redacts_bearer_and_sensitive_query_values(self):
        secret = "value-that-must-not-survive"
        message = (
            "Bearer " + secret + " https://example.test/file?" + "to" + "ken=" + secret
        )
        cleaned = hotmark.redact(message)
        self.assertNotIn(secret, cleaned)
        self.assertIn("REDACTED", cleaned)

    def test_logger_does_not_write_secret(self):
        secret = "never-write-this-value"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.log"
            logger = hotmark.build_logger(path)
            logger.info("Authorization: Bearer %s", secret)
            for handler in logger.handlers:
                handler.flush()
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertIn("REDACTED", content)
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

    def test_authentication_uses_current_endpoint_without_logging_credentials(self):
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": "temporary-access-token"}

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def post(self, url, **kwargs):
                captured["url"] = url
                captured["data"] = dict(kwargs["data"])
                captured["allow_redirects"] = kwargs["allow_redirects"]
                return FakeResponse()

        logger = MagicMock()
        with (
            patch("builtins.input", return_value="person@example.test"),
            patch.object(hotmark.getpass, "getpass", return_value="private-password"),
            patch.object(hotmark.requests, "Session", FakeSession),
            patch.object(hotmark, "LOGGER", logger),
        ):
            session, token = hotmark.authenticate()

        self.assertEqual(captured["url"], hotmark.AUTH_URL)
        self.assertEqual(
            captured["data"],
            {
                "username": "person@example.test",
                "password": "private-password",
                "grant_type": "password",
            },
        )
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(
            session.headers["Authorization"], "Bearer temporary-access-token"
        )
        self.assertEqual(token, {"token": "temporary-access-token"})

        log_text = " ".join(str(call) for call in logger.method_calls)
        self.assertNotIn("person@example.test", log_text)
        self.assertNotIn("private-password", log_text)
        self.assertNotIn("temporary-access-token", log_text)


class FileLayoutTests(unittest.TestCase):
    def test_windows_sanitization_preserves_accents(self):
        self.assertEqual(
            hotmark.sanitize_filename("Introdução: visão geral?"),
            "Introdução visão geral",
        )

    def test_duplicate_names_are_stable_and_numbered(self):
        seen = {}
        self.assertEqual(hotmark.unique_name("Apoio.pdf", seen), "Apoio.pdf")
        self.assertEqual(hotmark.unique_name("Apoio.pdf", seen), "Apoio (2).pdf")

    def test_encrypted_hls_is_rejected(self):
        raw = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"
#EXTINF:5,
segment.ts
"""
        playlist = m3u8.loads(raw)
        self.assertTrue(hotmark.playlist_is_encrypted(playlist, raw))

    def test_unencrypted_hls_is_allowed(self):
        raw = """#EXTM3U
#EXTINF:5,
segment.ts
"""
        playlist = m3u8.loads(raw)
        self.assertFalse(hotmark.playlist_is_encrypted(playlist, raw))

    def test_external_hls_host_never_receives_authenticated_session(self):
        authenticated = MagicMock()
        clean = MagicMock()
        expected = MagicMock()
        clean.get.return_value = expected
        with patch.object(hotmark.requests, "Session", return_value=clean):
            response = hotmark.hls_get(
                authenticated, "https://cdn.example.test/segment.ts"
            )
        self.assertIs(response, expected)
        authenticated.get.assert_not_called()
        clean.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
