#!/usr/bin/env python3
"""Smoke tests for platforms.bitbucket.BitbucketClient auth."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

from platforms.bitbucket import BitbucketClient, paginate
from platforms.errors import PlatformAuthError, PlatformError


class TestBitbucketClientAuth(unittest.TestCase):
    def test_access_token_uses_bearer(self):
        # No username + non-ATATT token → workspace/repo access token → Bearer.
        client = BitbucketClient("owner", "repo", token="secret")
        self.assertIsNone(client.session.auth)
        self.assertEqual(client.session.headers["Authorization"], "Bearer secret")

    def test_api_token_without_email_raises(self):
        # ATATT API token needs the Atlassian email; static user fails on REST.
        with self.assertRaises(PlatformAuthError):
            BitbucketClient("owner", "repo", token="ATATTsecret")

    def test_uses_basic_auth_when_username_provided(self):
        client = BitbucketClient("owner", "repo", token="secret", username="bob")
        self.assertEqual(client.session.auth, ("bob", "secret"))


class TestPaginate(unittest.TestCase):
    """`request_with_retry` returns None for both "404, nothing here" and
    "gave up after retries" -- paginate() must not conflate the two."""

    def test_not_found_on_first_page_returns_empty(self):
        session = requests.Session()
        with patch("platforms.bitbucket.request_with_retry", return_value=None):
            self.assertEqual(paginate(session, "https://api.bitbucket.org/2.0/x"), [])

    def test_failure_partway_through_raises_instead_of_truncating(self):
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "values": [{"id": 1}],
            "next": "https://api.bitbucket.org/2.0/x?page=2",
        }
        session = requests.Session()
        with patch(
            "platforms.bitbucket.request_with_retry", side_effect=[page1, None]
        ):
            with self.assertRaises(PlatformError):
                paginate(session, "https://api.bitbucket.org/2.0/x")

    def test_full_pagination_still_works(self):
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "values": [{"id": 1}],
            "next": "https://api.bitbucket.org/2.0/x?page=2",
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {"values": [{"id": 2}], "next": None}
        session = requests.Session()
        with patch(
            "platforms.bitbucket.request_with_retry", side_effect=[page1, page2]
        ):
            items = paginate(session, "https://api.bitbucket.org/2.0/x")
        self.assertEqual([i["id"] for i in items], [1, 2])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
