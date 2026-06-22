import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pipeline import run_full_pipeline


class PipelineIdentityTests(unittest.TestCase):
    def test_register_mismatch_is_non_fatal_and_uses_impressum_register(self):
        site_report = {
            "summary": {"data": {"name": "Example KG"}},
            "impressum": {
                "data": {
                    "company_name": "Example KG",
                    "register_number": "HRA 8631",
                    "register_court": "Amtsgericht Siegen",
                }
            },
        }

        with patch("pipeline.run_site_extractor", new=AsyncMock(return_value=site_report)):
            result = asyncio.run(
                run_full_pipeline(
                    "https://example.test",
                    with_profile=False,
                    skip_enrichment=True,
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


if __name__ == "__main__":
    unittest.main()
