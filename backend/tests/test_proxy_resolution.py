import unittest
from unittest import mock

from backend.integrations.proxy import (
    active_proxy_url,
    begin_sticky_proxy_account,
    bind_resin_sticky_account,
    clear_sticky_proxy_account,
    current_sticky_proxy_account,
    parse_http_proxy_url,
    redact_proxy_text,
    redact_proxy_url,
    resolve_proxy_url,
    validate_http_proxy_url,
)


class DockerProxyResolutionTests(unittest.TestCase):
    def test_localhost_proxy_maps_to_docker_host(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://127.0.0.1:7897"),
                "http://host.docker.internal:7897",
            )

    def test_credentials_are_preserved(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("socks5://user:pass@localhost:7897"),
                "socks5://user:pass@host.docker.internal:7897",
            )

    def test_encoded_http_credentials_are_preserved_during_host_rewrite(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://user%40mail:p%40ss%3Aword@localhost:7897"),
                "http://user%40mail:p%40ss%3Aword@host.docker.internal:7897",
            )

    def test_regular_proxy_is_unchanged(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://proxy.example.com:7897"),
                "http://proxy.example.com:7897",
            )


class HttpProxyParsingTests(unittest.TestCase):
    def test_authenticated_http_proxy_is_split_for_camoufox(self):
        self.assertEqual(
            parse_http_proxy_url("http://user:password@proxy.example.com:8080"),
            {
                "server": "http://proxy.example.com:8080",
                "username": "user",
                "password": "password",
            },
        )

    def test_percent_encoded_credentials_are_decoded(self):
        self.assertEqual(
            parse_http_proxy_url(
                "https://user%40mail.example:p%40ss%3Aword@proxy.example.com:8443"
            ),
            {
                "server": "https://proxy.example.com:8443",
                "username": "user@mail.example",
                "password": "p@ss:word",
            },
        )

    def test_original_encoded_url_is_retained_for_http_clients(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(validate_http_proxy_url(proxy), proxy)

    def test_invalid_percent_encoding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad%ZZ@proxy.example.com:8080")

    def test_unencoded_path_character_in_credentials_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad/word@proxy.example.com:8080")

    def test_proxy_credentials_are_redacted_for_display_and_log_text(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(
            redact_proxy_url(proxy),
            "http://***:***@proxy.example.com:8080",
        )
        message = redact_proxy_text(f"request failed via {proxy}")
        self.assertNotIn("user%40mail", message)
        self.assertNotIn("p%40ss", message)
        self.assertIn("http://***:***@proxy.example.com:8080", message)

        malformed = redact_proxy_text(
            "failed via http://user:raw/secret@proxy.example.com:8080"
        )
        self.assertNotIn("raw/secret", malformed)


class ResinStickyAccountTests(unittest.TestCase):
    def tearDown(self):
        clear_sticky_proxy_account()

    def test_platform_username_gains_one_account(self):
        bound = bind_resin_sticky_account(
            "http://1024proxy:secret@127.0.0.1:2260",
            "rabc1234",
        )
        self.assertEqual(bound, "http://1024proxy.rabc1234:secret@127.0.0.1:2260")
        self.assertEqual(
            parse_http_proxy_url(bound)["username"],
            "1024proxy.rabc1234",
        )

    def test_existing_account_is_left_in_place(self):
        original = "http://1024proxy.manual:secret@127.0.0.1:2260"
        self.assertEqual(bind_resin_sticky_account(original, "rother"), original)

    def test_encoded_password_stays_encoded(self):
        bound = bind_resin_sticky_account(
            "http://1024proxy:p%40ss@proxy.example.com:2260",
            "rabc1234",
        )
        self.assertEqual(
            bound,
            "http://1024proxy.rabc1234:p%40ss@proxy.example.com:2260",
        )

    def test_active_proxy_uses_the_thread_account_after_host_rewrite(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            account = begin_sticky_proxy_account()
            self.assertEqual(current_sticky_proxy_account(), account)
            resolved = active_proxy_url("http://1024proxy:secret@127.0.0.1:2260")
            self.assertEqual(
                resolved,
                f"http://1024proxy.{account}:secret@host.docker.internal:2260",
            )
            clear_sticky_proxy_account()
            self.assertEqual(
                active_proxy_url("http://1024proxy:secret@127.0.0.1:2260"),
                "http://1024proxy:secret@host.docker.internal:2260",
            )


if __name__ == "__main__":
    unittest.main()
