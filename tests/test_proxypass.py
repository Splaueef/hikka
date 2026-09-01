import asyncio
import sys
import types
import unittest


# proxypass only needs this callback from the dependency-heavy utility module.
utils_stub = types.ModuleType("hikka.utils")
utils_stub.atexit = lambda _callback: None
sys.modules.setdefault("hikka.utils", utils_stub)

from hikka.web.proxypass import ProxyPasser, _extract_tunnel_url


class ExtractTunnelUrlTest(unittest.TestCase):
    def test_ignores_localhost_run_link(self):
        self.assertIsNone(
            _extract_tunnel_url("Manage your tunnels at https://admin.localhost.run")
        )

    def test_extracts_generated_tunnel_link(self):
        self.assertEqual(
            _extract_tunnel_url(
                "tunneled with tls termination, https://example-session.lhr.life"
            ),
            "https://example-session.lhr.life",
        )

    def test_uses_lhr_tunnel_when_line_also_contains_admin_link(self):
        self.assertEqual(
            _extract_tunnel_url(
                "https://admin.localhost.run -> https://random-name.lhr.life/"
            ),
            "https://random-name.lhr.life",
        )

    def test_ignores_other_localhost_run_subdomains(self):
        self.assertIsNone(_extract_tunnel_url("https://random-name.localhost.run"))

    def test_ignores_lhr_life_suffix_on_another_domain(self):
        self.assertIsNone(_extract_tunnel_url("https://name.lhr.life.example.com"))


class ProcessStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_actual_tunnel_url(self):
        proxy = ProxyPasser()
        proxy._url_available = asyncio.Event()

        await proxy._process_stream("Visit https://admin.localhost.run for help")
        self.assertFalse(proxy._url_available.is_set())
        self.assertIsNone(proxy._tunnel_url)

        await proxy._process_stream("Your URL is https://working-tunnel.lhr.life")
        self.assertTrue(proxy._url_available.is_set())
        self.assertEqual(proxy._tunnel_url, "https://working-tunnel.lhr.life")


if __name__ == "__main__":
    unittest.main()
