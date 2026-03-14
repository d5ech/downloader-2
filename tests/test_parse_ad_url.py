"""
tests/test_parse_ad_url.py

Unit tests for backend.utils.parse_ad_url.
Run with:  pytest tests/test_parse_ad_url.py -v
"""

from __future__ import annotations

import pytest
import sys
import os

# Make sure the repo root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.utils import parse_ad_url, AdUrlError

# ---------------------------------------------------------------------------
# Shared constant
# ---------------------------------------------------------------------------

VALID_ID = "1457638199042352"
EXPECTED_URL = f"https://www.facebook.com/ads/library/?id={VALID_ID}"

# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestValidInputs:
    def test_full_url_with_id_param(self):
        result = parse_ad_url(f"https://www.facebook.com/ads/library/?id={VALID_ID}")
        assert result == {"ad_id": VALID_ID, "url": EXPECTED_URL}

    def test_bare_numeric_id(self):
        result = parse_ad_url(VALID_ID)
        assert result == {"ad_id": VALID_ID, "url": EXPECTED_URL}

    def test_url_leading_trailing_whitespace(self):
        result = parse_ad_url(f"  https://www.facebook.com/ads/library/?id={VALID_ID}  ")
        assert result["ad_id"] == VALID_ID

    def test_bare_id_leading_trailing_whitespace(self):
        result = parse_ad_url(f"  {VALID_ID}  ")
        assert result["ad_id"] == VALID_ID

    def test_url_extra_query_params(self):
        """Extra query parameters must not affect parsing."""
        url = (
            f"https://www.facebook.com/ads/library/"
            f"?active_status=all&id={VALID_ID}&country=US"
        )
        result = parse_ad_url(url)
        assert result["ad_id"] == VALID_ID

    def test_url_with_ad_id_param(self):
        """Also accept the legacy 'ad_id' query parameter name."""
        url = f"https://www.facebook.com/ads/library/?ad_id={VALID_ID}"
        result = parse_ad_url(url)
        assert result["ad_id"] == VALID_ID

    def test_canonical_url_always_uses_id_param(self):
        """Canonical URL must always use ?id=, regardless of source param name."""
        url = f"https://www.facebook.com/ads/library/?ad_id={VALID_ID}"
        result = parse_ad_url(url)
        assert result["url"] == EXPECTED_URL

    def test_www_prefix_optional(self):
        result = parse_ad_url(f"https://facebook.com/ads/library/?id={VALID_ID}")
        assert result["ad_id"] == VALID_ID

    def test_http_scheme_accepted(self):
        """http:// links should be handled (the canonical URL returned is https)."""
        result = parse_ad_url(f"http://www.facebook.com/ads/library/?id={VALID_ID}")
        assert result["ad_id"] == VALID_ID
        assert result["url"].startswith("https://")

    def test_minimum_length_id(self):
        result = parse_ad_url("1")
        assert result["ad_id"] == "1"

    def test_maximum_length_id(self):
        long_id = "1" * 20
        result = parse_ad_url(long_id)
        assert result["ad_id"] == long_id

    def test_return_type_is_dict(self):
        result = parse_ad_url(VALID_ID)
        assert isinstance(result, dict)

    def test_return_keys(self):
        result = parse_ad_url(VALID_ID)
        assert set(result.keys()) == {"ad_id", "url"}

    def test_return_values_are_strings(self):
        result = parse_ad_url(VALID_ID)
        assert isinstance(result["ad_id"], str)
        assert isinstance(result["url"], str)

    def test_url_field_is_canonical_facebook_url(self):
        result = parse_ad_url(VALID_ID)
        assert result["url"].startswith("https://www.facebook.com/ads/library/?id=")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestInvalidInputs:
    def test_empty_string_raises(self):
        with pytest.raises(AdUrlError, match="empty"):
            parse_ad_url("")

    def test_whitespace_only_raises(self):
        with pytest.raises(AdUrlError, match="empty"):
            parse_ad_url("   ")

    def test_non_string_raises(self):
        with pytest.raises(AdUrlError, match="string"):
            parse_ad_url(1457638199042352)  # type: ignore[arg-type]

    def test_none_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url(None)  # type: ignore[arg-type]

    def test_alphanumeric_id_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("abc123")

    def test_id_with_spaces_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("1457638 199042352")

    def test_id_with_dashes_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("1457638-199042352")

    def test_id_too_long_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("1" * 21)

    def test_wrong_domain_raises(self):
        with pytest.raises(AdUrlError, match="facebook.com"):
            parse_ad_url(f"https://evil.com/ads/library/?id={VALID_ID}")

    def test_wrong_path_raises(self):
        with pytest.raises(AdUrlError, match="/ads/library"):
            parse_ad_url(f"https://www.facebook.com/some/other/path/?id={VALID_ID}")

    def test_url_missing_id_param_raises(self):
        with pytest.raises(AdUrlError, match="No 'id'"):
            parse_ad_url("https://www.facebook.com/ads/library/?country=US")

    def test_url_with_non_numeric_id_raises(self):
        with pytest.raises(AdUrlError, match="numeric"):
            parse_ad_url("https://www.facebook.com/ads/library/?id=abc123")

    def test_float_string_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("1457638199.042352")

    def test_url_with_id_param_empty_value_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("https://www.facebook.com/ads/library/?id=")

    def test_random_string_raises(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("not-a-url-or-id")


# ---------------------------------------------------------------------------
# AdUrlError is a subclass of ValueError
# ---------------------------------------------------------------------------


class TestAdUrlErrorType:
    def test_is_value_error(self):
        with pytest.raises(ValueError):
            parse_ad_url("")

    def test_is_ad_url_error(self):
        with pytest.raises(AdUrlError):
            parse_ad_url("")
