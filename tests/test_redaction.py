from __future__ import annotations

import unittest

from core.redaction import (
    REDACTED,
    configure_redaction_secrets,
    redact_text,
    redact_value,
)


class SecretObject:
    def __str__(self) -> str:
        return "Authorization: Bearer object-secret"


class RedactionTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_redaction_secrets(())

    def test_redact_text_covers_urls_headers_tokens_and_json(self) -> None:
        secret = "seeded-super-secret"
        configure_redaction_secrets((secret,))
        text = (
            "wss://example.test/ws?access_key=alpha&safe=visible&TICKET=beta "
            "Authorization: Bearer bearer-value\n"
            "Proxy-Authorization: Basic basic-value\n"
            'Cookie: session=cookie-value\n'
            '{"client_secret":"json-secret","nested":{"apiKey":"camel-secret"},'
            '"ordinary":"visible"} '
            f"literal={secret}"
        )

        redacted = redact_text(text)

        for value in (
            "alpha",
            "beta",
            "bearer-value",
            "basic-value",
            "cookie-value",
            "json-secret",
            "camel-secret",
            secret,
        ):
            self.assertNotIn(value, redacted)
        self.assertIn("safe=visible", redacted)
        self.assertIn('"ordinary":"visible"', redacted)
        self.assertGreaterEqual(redacted.count(REDACTED), 7)

    def test_redact_text_is_idempotent_and_handles_repeated_values(self) -> None:
        value = "token=repeated&access_token=repeated"

        first = redact_text(value)
        second = redact_text(first)

        self.assertEqual(first, second)
        self.assertNotIn("repeated", first)

    def test_redact_value_preserves_shape_and_redacts_sensitive_keys(self) -> None:
        value = {
            "safe": "visible",
            "token": "top-secret",
            "nested": [
                {"Authorization": "Bearer nested-secret"},
                ("ticket=tuple-secret", 3),
            ],
        }

        redacted = redact_value(value)

        self.assertEqual(redacted["safe"], "visible")
        self.assertEqual(redacted["token"], REDACTED)
        self.assertEqual(redacted["nested"][0]["Authorization"], REDACTED)
        self.assertIsInstance(redacted["nested"][1], tuple)
        self.assertNotIn("tuple-secret", redacted["nested"][1][0])

    def test_redact_value_is_cycle_safe_and_non_throwing(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)

        redacted = redact_value(
            {
                "cycle": cyclic,
                "object": SecretObject(),
            },
            max_depth=3,
            max_nodes=10,
        )

        self.assertEqual(redacted["cycle"][0], "[CIRCULAR]")
        self.assertNotIn("object-secret", redacted["object"])

    def test_redact_value_bounds_deep_and_wide_values(self) -> None:
        deep: dict[str, object] = {}
        cursor = deep
        for index in range(10):
            child: dict[str, object] = {}
            cursor[f"level_{index}"] = child
            cursor = child
        wide = list(range(20))

        deep_result = redact_value(deep, max_depth=2)
        wide_result = redact_value(wide, max_nodes=3)

        self.assertIn("[MAX_DEPTH]", repr(deep_result))
        self.assertIn("[MAX_NODES]", repr(wide_result))


if __name__ == "__main__":
    unittest.main()
