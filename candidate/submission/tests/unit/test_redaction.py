"""Redaction must hold for messages this code did not write.

The contract forbids logging tokens or contacts and requires redacting them in
error messages too. Per-call-site discipline cannot cover a library exception
that interpolates a URL, so the filter is tested against the shapes that
actually leak.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.obs.logging import JsonFormatter, redact


class TestRedact:
    @pytest.mark.parametrize("text", [
        "contact person00000@aster.example.invalid failed",
        "sent to first.last+tag@example.co.uk",
    ])
    def test_contacts_are_removed(self, text):
        assert "@" not in redact(text)
        assert "[redacted]" in redact(text)

    def test_bearer_tokens_are_removed(self):
        token = "aB3-x_9YqLmNoPqRsTuVwXyZ0123456789"  # fake-credential-for-test
        out = redact(f"Authorization: Bearer {token}")
        assert token not in out

    @pytest.mark.parametrize("text", [
        'token: sk-live-9YqLmNoPqRsTuVwXyZ0123456789abcdef',
        'password=hunter2-with-a-long-tail-value',
        '"api_key": "9YqLmNoPqRsTuVwXyZ0123456789abcdef"',
    ])
    def test_labelled_secrets_are_removed(self, text):
        assert "[redacted]" in redact(text)

    def test_bare_high_entropy_strings_are_removed(self):
        token = "Zx9" + "a1B2c3D4e5F6g7H8i9J0" * 3
        assert token not in redact(f"unexpected value {token} in response")

    def test_our_own_lineage_hashes_survive(self):
        # A sha256 is 64 hex characters and is deliberately not a secret; losing
        # it would make the lineage log useless.
        digest = "f8fa9bb6e4e4af93f167e1a8346540fa0f1078bac645f29d7fe13a76177191ed"
        assert digest in redact(f"payload_sha256={digest}")

    def test_ordinary_text_is_untouched(self):
        text = "page applied source=aster cursor=3134 accepted=200 rejected=0"
        assert redact(text) == text


class TestJsonFormatter:
    @staticmethod
    def _emit(msg: str, **extra) -> dict:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
        for key, value in extra.items():
            setattr(record, key, value)
        return json.loads(JsonFormatter().format(record))

    def test_message_is_redacted(self):
        out = self._emit("failed for person00000@aster.example.invalid")
        assert "@" not in out["msg"]

    def test_structured_fields_are_redacted_too(self):
        out = self._emit("poll failed", source="birch",
                         detail="contact person1@x.example.invalid rejected")
        assert out["source"] == "birch"
        assert "@" not in out["detail"]

    def test_nested_structures_are_redacted(self):
        out = self._emit("quarantined", detail={"fields": ["a@b.example.invalid"]})
        assert "@" not in json.dumps(out["detail"])

    def test_exception_text_is_redacted(self):
        try:
            raise ValueError("bad token Bearer aB3x9YqLmNoPqRsTuVwXyZ0123456789")  # fake-credential-for-test
        except ValueError:
            record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None,
                                       __import__("sys").exc_info())
        out = json.loads(JsonFormatter().format(record))
        assert "aB3x9YqLmNoPqRsTuVwXyZ0123456789" not in out["exc"]

    def test_output_is_one_json_line(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "multi\nline", None, None)
        rendered = JsonFormatter().format(record)
        assert "\n" not in rendered
        assert json.loads(rendered)["msg"] == "multi\nline"
