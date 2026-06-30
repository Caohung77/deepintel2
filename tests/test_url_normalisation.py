import unittest

from api.server import normalise_url
from worker import _ensure_scheme


class UrlNormalisationTests(unittest.TestCase):
    def test_api_unwraps_markdown_url(self):
        self.assertEqual(
            normalise_url("[https://kanzlei-distel.de/](https://kanzlei-distel.de/)"),
            "https://kanzlei-distel.de/",
        )

    def test_worker_unwraps_markdown_url(self):
        self.assertEqual(
            _ensure_scheme("[https://kanzlei-distel.de/](https://kanzlei-distel.de/)"),
            "https://kanzlei-distel.de/",
        )


if __name__ == "__main__":
    unittest.main()
