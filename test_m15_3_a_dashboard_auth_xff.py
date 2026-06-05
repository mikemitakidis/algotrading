"""test_m15_3_a_dashboard_auth_xff.py — P0-1 trusted-proxy tests.

Verifies dashboard.auth.trusted_proxy.resolve_client_ip honours
X-Forwarded-For ONLY when remote_addr is in the trusted-proxy
allowlist, and uses the LAST entry of XFF in that case (the hop
immediately before our trusted proxy = the real client).

Recorded at the M1-M16 audit pass; mitigates the rate-limit-bypass
+ audit-IP-corruption finding (audit P0-1).
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from dashboard.auth.trusted_proxy import (
    DEFAULT_TRUSTED_PROXIES,
    resolve_client_ip,
    _read_trusted_proxies_env,
    _pick_last_real_ip_from_xff,
    _is_trusted_proxy,
)


# ─────────────────────────────────────────────────────────────────────────────
# G1. The pure resolver
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveClientIP(unittest.TestCase):

    def test_xff_from_loopback_is_trusted(self):
        """remote_addr=127.0.0.1 + XFF=1.2.3.4 → client = 1.2.3.4"""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="1.2.3.4")
        self.assertEqual(r, "1.2.3.4")

    def test_xff_from_ipv6_loopback_is_trusted(self):
        """remote_addr=::1 + XFF=1.2.3.4 → client = 1.2.3.4"""
        r = resolve_client_ip(remote_addr="::1",
                              xff_header="1.2.3.4")
        self.assertEqual(r, "1.2.3.4")

    def test_xff_from_untrusted_source_is_ignored(self):
        """remote_addr=10.20.30.40 + XFF=1.2.3.4 → client = 10.20.30.40"""
        r = resolve_client_ip(remote_addr="10.20.30.40",
                              xff_header="1.2.3.4")
        self.assertEqual(r, "10.20.30.40")

    def test_xff_chain_takes_last_not_first(self):
        """remote_addr=127.0.0.1 + XFF='attacker, 1.2.3.4'
        → client = 1.2.3.4 (the last hop, NOT the leftmost which is
        attacker-controllable)"""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="9.9.9.9, 5.5.5.5, 1.2.3.4")
        self.assertEqual(r, "1.2.3.4")

    def test_xff_with_no_header_falls_back_to_remote_addr(self):
        """No XFF + trusted remote_addr → use remote_addr"""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="")
        self.assertEqual(r, "127.0.0.1")

    def test_malformed_xff_ignored(self):
        """Whitespace-only XFF on trusted source → fall back to remote_addr"""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="   ,  , ,")
        self.assertEqual(r, "127.0.0.1")

    def test_xff_none_header_falls_back(self):
        """XFF=None on trusted source → fall back to remote_addr"""
        r = resolve_client_ip(remote_addr="127.0.0.1", xff_header=None)
        self.assertEqual(r, "127.0.0.1")

    def test_both_none_returns_empty(self):
        """Truly no source available → empty string (not raise)"""
        r = resolve_client_ip(remote_addr=None, xff_header=None)
        self.assertEqual(r, "")

    def test_none_remote_addr_with_xff_returns_empty(self):
        """None remote_addr is NOT in trusted list → ignore XFF →
        fall back to remote_addr which is None → empty string"""
        r = resolve_client_ip(remote_addr=None, xff_header="1.2.3.4")
        self.assertEqual(r, "")

    def test_env_override_widens_allowlist(self):
        """Explicit trusted_proxies arg overrides env."""
        r = resolve_client_ip(remote_addr="10.0.0.1",
                              xff_header="1.2.3.4",
                              trusted_proxies=("10.0.0.1",))
        self.assertEqual(r, "1.2.3.4")

    def test_env_override_excludes_default_loopback(self):
        """If trusted_proxies is explicitly passed, the defaults are
        NOT in effect — so loopback becomes untrusted in this scope."""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="1.2.3.4",
                              trusted_proxies=("10.0.0.1",))
        self.assertEqual(r, "127.0.0.1")  # loopback NOT in supplied list

    def test_single_chain_entry_works(self):
        """remote_addr=127.0.0.1 + XFF='5.5.5.5' (one hop) → 5.5.5.5"""
        r = resolve_client_ip(remote_addr="127.0.0.1",
                              xff_header="5.5.5.5")
        self.assertEqual(r, "5.5.5.5")


# ─────────────────────────────────────────────────────────────────────────────
# G2. Env var parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestReadTrustedProxiesEnv(unittest.TestCase):

    def test_unset_returns_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHBOARD_TRUSTED_PROXIES", None)
            self.assertEqual(_read_trusted_proxies_env(),
                             DEFAULT_TRUSTED_PROXIES)

    def test_empty_returns_defaults(self):
        with patch.dict(os.environ, {"DASHBOARD_TRUSTED_PROXIES": ""}):
            self.assertEqual(_read_trusted_proxies_env(),
                             DEFAULT_TRUSTED_PROXIES)

    def test_whitespace_returns_defaults(self):
        with patch.dict(os.environ, {"DASHBOARD_TRUSTED_PROXIES": "   "}):
            self.assertEqual(_read_trusted_proxies_env(),
                             DEFAULT_TRUSTED_PROXIES)

    def test_single_ip_parses(self):
        with patch.dict(os.environ,
                          {"DASHBOARD_TRUSTED_PROXIES": "10.0.0.1"}):
            self.assertEqual(_read_trusted_proxies_env(), ("10.0.0.1",))

    def test_comma_separated_parses(self):
        with patch.dict(os.environ,
                          {"DASHBOARD_TRUSTED_PROXIES": "10.0.0.1,10.0.0.2"}):
            self.assertEqual(_read_trusted_proxies_env(),
                             ("10.0.0.1", "10.0.0.2"))

    def test_whitespace_in_csv_tolerated(self):
        with patch.dict(os.environ,
                          {"DASHBOARD_TRUSTED_PROXIES":
                              "  10.0.0.1 ,  10.0.0.2  "}):
            self.assertEqual(_read_trusted_proxies_env(),
                             ("10.0.0.1", "10.0.0.2"))

    def test_env_only_commas_returns_defaults(self):
        with patch.dict(os.environ, {"DASHBOARD_TRUSTED_PROXIES": ",,,"}):
            self.assertEqual(_read_trusted_proxies_env(),
                             DEFAULT_TRUSTED_PROXIES)


# ─────────────────────────────────────────────────────────────────────────────
# G3. Helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):

    def test_is_trusted_loopback(self):
        self.assertTrue(_is_trusted_proxy("127.0.0.1", ("127.0.0.1",)))
        self.assertTrue(_is_trusted_proxy("::1", ("127.0.0.1", "::1")))

    def test_is_trusted_none_or_empty(self):
        self.assertFalse(_is_trusted_proxy(None, ("127.0.0.1",)))
        self.assertFalse(_is_trusted_proxy("", ("127.0.0.1",)))

    def test_is_trusted_not_in_list(self):
        self.assertFalse(_is_trusted_proxy("10.20.30.40", ("127.0.0.1",)))

    def test_pick_last_real_ip_simple(self):
        self.assertEqual(_pick_last_real_ip_from_xff("1.2.3.4"), "1.2.3.4")

    def test_pick_last_real_ip_chain(self):
        self.assertEqual(
            _pick_last_real_ip_from_xff("a, b, 1.2.3.4"), "1.2.3.4")

    def test_pick_last_real_ip_empty(self):
        self.assertIsNone(_pick_last_real_ip_from_xff(""))
        self.assertIsNone(_pick_last_real_ip_from_xff(",  ,  "))
        self.assertIsNone(_pick_last_real_ip_from_xff("   "))


# ─────────────────────────────────────────────────────────────────────────────
# G4. Integration: rate-limiter keys on real source, not spoofed XFF
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimiterKeysOnRealSource(unittest.TestCase):
    """The regression test the audit P0-1 finding demanded.

    Issue N login attempts from a single untrusted source while
    rotating the X-Forwarded-For header each time. The rate-limiter
    keys on `_m153a_client_ip()` — which, post-fix, returns the
    untrusted `remote_addr` (the SAME value every request), so the
    threshold IS reached and lockout DOES trigger.

    Before the fix, each spoofed XFF was returned verbatim → each
    attempt was keyed on a different IP → threshold never reached.
    """

    def test_rate_limiter_keys_on_real_source_not_xff(self):
        # We test via the trusted_proxy resolver directly + a fresh
        # RateLimiter — simulating what _m153a_client_ip would return
        # for each request. No Flask context needed.
        from dashboard.auth.rate_limit import RateLimiter, LoginRateLimited

        limiter = RateLimiter(window_sec=600, threshold=5,
                                lockout_sec=900)

        # All six requests come from the same untrusted source
        # 10.20.30.40, but each request claims a different XFF.
        spoofed_xffs = [
            "1.1.1.1",
            "2.2.2.2",
            "3.3.3.3",
            "4.4.4.4",
            "5.5.5.5",
            "6.6.6.6",
        ]

        # Pre-fix behaviour SIMULATION (for context — what would
        # happen if we used the spoofed XFF as the key):
        for xff in spoofed_xffs[:5]:
            limiter.record_failure(xff)  # five different keys
        # No single key reached threshold → no lockout.
        for xff in spoofed_xffs:
            # Should NOT raise — different keys each.
            limiter.check_locked(xff)

        # Now reset and run the POST-fix behaviour: every request
        # keyed on the trusted-resolved IP (the real remote_addr).
        limiter2 = RateLimiter(window_sec=600, threshold=5,
                                 lockout_sec=900)
        real_source = "10.20.30.40"
        for xff in spoofed_xffs:
            resolved = resolve_client_ip(remote_addr=real_source,
                                          xff_header=xff)
            self.assertEqual(resolved, real_source,
                             f"spoofed XFF leaked through for {xff!r}")
            # Don't record the 6th — we check_locked first to assert
            # lockout actually kicks in after the 5th.
            if xff != spoofed_xffs[-1]:
                limiter2.record_failure(resolved)
            else:
                # Sixth attempt — check_locked must raise.
                with self.assertRaises(LoginRateLimited):
                    limiter2.check_locked(resolved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
