import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fast_extractor import (
    FetchResult,
    _effective_register_input,
    _extract_register_from_impressum_text,
    fast_extract,
)


class FastRegisterExtractionTests(unittest.TestCase):
    def test_extracts_register_from_impressum_text(self):
        text = """
        Jordan GmbH
        Handelsregister: HRB 2537
        Amtsgericht Braunschweig
        USt-IdNr.: DE123456789
        """

        self.assertEqual(
            _extract_register_from_impressum_text(text),
            {"register_number": "HRB 2537", "register_court": "Braunschweig"},
        )

    def test_impressum_register_is_effective_when_user_left_fields_empty(self):
        result = _effective_register_input(
            "",
            "",
            {"register_number": "HRB 2537", "register_court": "Braunschweig"},
        )

        self.assertEqual(result, {"hr_no": "HRB 2537", "register_court": "Braunschweig"})

    def test_impressum_register_can_override_mismatched_user_input(self):
        result = _effective_register_input(
            "HRB 6327",
            "Siegen",
            {"register_number": "HRA 8631", "register_court": "Amtsgericht Siegen"},
            prefer_impressum=True,
        )

        self.assertEqual(
            result,
            {"hr_no": "HRA 8631", "register_court": "Amtsgericht Siegen"},
        )

    def test_register_mismatch_is_non_fatal_for_url_request(self):
        home_html = f"<html><body><p>{'Example company text. ' * 20}</p></body></html>"
        impressum_html = "<html><body>Handelsregister: HRA 8631 Amtsgericht Siegen</body></html>"

        with (
            patch(
                "fast_extractor.fetch_html",
                new=AsyncMock(
                    side_effect=[
                        FetchResult(html=home_html, status=200, final_url="https://example.test"),
                        FetchResult(html=impressum_html, status=200, final_url="https://example.test/impressum"),
                    ]
                ),
            ),
            patch("fast_extractor.find_impressum_url", new=AsyncMock(return_value="https://example.test/impressum")),
            patch("fast_extractor.llm_extract", new=AsyncMock(return_value={"name": "Example KG"})),
            patch(
                "fast_extractor.llm_extract_impressum",
                new=AsyncMock(
                    return_value={
                        "company_name": "Example KG",
                        "register_number": "HRA 8631",
                        "register_court": "Amtsgericht Siegen",
                    }
                ),
            ),
        ):
            result = asyncio.run(
                fast_extract(
                    "https://example.test",
                    with_profile=False,
                    with_enrichment=False,
                    with_branch=False,
                    hr_no="HRB 6327",
                    register_court="Siegen",
                )
            )

        self.assertNotIn("error_kind", result)
        self.assertEqual(
            result["register_input"],
            {"hr_no": "HRA 8631", "register_court": "Amtsgericht Siegen"},
        )
        self.assertEqual(result["identity_match"]["verified"], False)

    def test_https_transport_failure_retries_plain_http_redirect(self):
        home_html = f"<html><body><p>{'Distel company text. ' * 20}</p></body></html>"
        impressum_html = "<html><body>Dr. Distel GmbH HRB 765167 Amtsgericht Stuttgart</body></html>"
        fetch_mock = AsyncMock(
            side_effect=[
                FetchResult(error_kind="dns", error_detail="TLS alert"),
                FetchResult(
                    html=home_html,
                    status=200,
                    final_url="https://www.kanzlei-distel.com/",
                ),
                FetchResult(
                    html=impressum_html,
                    status=200,
                    final_url="https://www.kanzlei-distel.com/Impressum",
                ),
            ]
        )
        find_impressum_mock = AsyncMock(
            return_value="https://www.kanzlei-distel.com/Impressum"
        )

        with (
            patch("fast_extractor.fetch_html", new=fetch_mock),
            patch("fast_extractor.find_impressum_url", new=find_impressum_mock),
            patch("fast_extractor.llm_extract", new=AsyncMock(return_value={"name": "Dr. Distel GmbH"})),
            patch(
                "fast_extractor.llm_extract_impressum",
                new=AsyncMock(
                    return_value={
                        "company_name": "Dr. Distel GmbH",
                        "register_number": "HRB 765167",
                        "register_court": "Stuttgart",
                    }
                ),
            ),
        ):
            result = asyncio.run(
                fast_extract(
                    "https://www.kanzlei-distel.de/",
                    with_profile=False,
                    with_enrichment=False,
                    with_branch=False,
                )
            )

        self.assertNotIn("error_kind", result)
        self.assertEqual(result["source_url"], "https://www.kanzlei-distel.com/")
        self.assertEqual(fetch_mock.call_args_list[0].args[0], "https://www.kanzlei-distel.de/")
        self.assertEqual(fetch_mock.call_args_list[1].args[0], "http://www.kanzlei-distel.de/")
        self.assertEqual(find_impressum_mock.call_args.args[0], "https://www.kanzlei-distel.com/")


if __name__ == "__main__":
    unittest.main()
