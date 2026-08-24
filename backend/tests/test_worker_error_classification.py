"""
Fehlerklassifizierung im Worker.

Bot-Check und Altersbeschränkung sehen sich in yt-dlps Ausgabe zum
Verwechseln ähnlich — beide beginnen mit "Sign in to confirm you…". Nur der
erste Fall ist transient. Wird der zweite mitgefangen, verbrennt jeder
altersbeschränkte Track fünf Retries samt langem Lane-Cooldown, ohne je
erfolgreich werden zu können.
"""

from __future__ import annotations

from utils.worker import _looks_like_age_restriction, _looks_like_bot_check

BOT_CHECK = (
    "ERROR: [youtube] 0SycsfGzH60: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)
AGE_PLAIN = (
    "ERROR: [youtube] YBeCF79eloo: Sign in to confirm your age. "
    "Use --cookies-from-browser or --cookies for the authentication."
)
AGE_INAPPROPRIATE = (
    "ERROR: [youtube] NDb6OT8Dp3A: Sign in to confirm your age. "
    "This video may be inappropriate for some users."
)


def test_bot_check_is_recognised():
    assert _looks_like_bot_check(BOT_CHECK, "") is True
    assert _looks_like_age_restriction(BOT_CHECK, "") is False


def test_age_restriction_is_not_a_bot_check():
    """Die Regression: 'confirm your age' darf nicht in den Retry-Pfad."""
    assert _looks_like_age_restriction(AGE_PLAIN, "") is True
    assert _looks_like_bot_check(AGE_PLAIN, "") is False


def test_age_restriction_inappropriate_variant():
    assert _looks_like_age_restriction(AGE_INAPPROPRIATE, "") is True
    assert _looks_like_bot_check(AGE_INAPPROPRIATE, "") is False


def test_error_field_is_considered_too():
    """Beide Helfer lesen message UND error."""
    assert _looks_like_age_restriction("", AGE_PLAIN) is True
    assert _looks_like_bot_check("", BOT_CHECK) is True


def test_unrelated_failures_match_neither():
    for msg in ("HTTP Error 403: Forbidden", "This video is DRM protected", ""):
        assert _looks_like_bot_check(msg, "") is False
        assert _looks_like_age_restriction(msg, "") is False
