import unittest

from enrichment.tavily_client import _classify_insolvency


class InsolvencyIdentityTests(unittest.TestCase):
    def test_similar_company_name_does_not_trigger_insolvency(self):
        result = _classify_insolvency(
            "Elektro Technik Jordan GmbH",
            "",
            [
                {
                    "title": "Elektro Jordan GmbH: Insolvenzverfahren eroeffnet",
                    "snippet": "Elektro Jordan GmbH, Amtsgericht Musterstadt, Az. 2 IN 277/26.",
                    "url": "https://example.test/elektro-jordan",
                }
            ],
        )

        self.assertFalse(result["insolvenzverfahren"])
        self.assertEqual(result["evidence"], [])

    def test_hyphenated_same_company_name_still_triggers_insolvency(self):
        result = _classify_insolvency(
            "Elektrotechnik Jordan GmbH",
            "",
            [
                {
                    "title": "Elektro-Technik Jordan GmbH: Insolvenzverfahren eroeffnet",
                    "snippet": "Fuer Elektro-Technik Jordan GmbH wurde ein vorlaeufiger Insolvenzverwalter bestellt.",
                    "url": "https://example.test/elektrotechnik-jordan",
                }
            ],
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(len(result["evidence"]), 1)

    def test_title_only_pages_are_not_added_as_evidence(self):
        result = _classify_insolvency(
            "Elektrotechnik Jordan GmbH",
            "",
            [
                {
                    "title": "Elektrotechnik Jordan GmbH: Insolvenzverfahren eroeffnet",
                    "snippet": "Elektrotechnik Jordan GmbH, Az. 2 IN 277/26.",
                    "url": "https://example.test/valid",
                },
                {
                    "title": "Elektrotechnik Jordan GmbH Firmenprofil",
                    "snippet": "Adresse, Telefonnummer und Unternehmensdaten.",
                    "url": "https://example.test/profile",
                },
            ],
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(
            [e["url"] for e in result["evidence"]],
            ["https://example.test/valid"],
        )

    def test_jordan_gmbh_rejects_different_legal_entities(self):
        result = _classify_insolvency(
            "Jordan GmbH",
            "",
            [
                {
                    "title": "Firmeninsolvenz G. Jordan GmbH & Co. KG - Bad Arolsen",
                    "snippet": "Fuer G. Jordan GmbH & Co. KG wurde ein Insolvenzverfahren eroeffnet.",
                    "url": "https://example.test/g-jordan",
                },
                {
                    "title": "Jordan Capital GmbH - Moenchengladbach - Insolvenz-Radar",
                    "snippet": "Jordan Capital GmbH, Amtsgericht Moenchengladbach, Az. 2 IN 277/26.",
                    "url": "https://example.test/jordan-capital",
                },
                {
                    "title": "INSOLVENZRADAR.de(BETA) - Alle Insolvenzmeldungen im Blick",
                    "snippet": "Aktuelle Insolvenzmeldungen und Bekanntmachungen.",
                    "url": "https://example.test/generic",
                },
            ],
        )

        self.assertFalse(result["insolvenzverfahren"])
        self.assertEqual(result["evidence"], [])

    def test_jordan_gmbh_exact_legal_name_triggers(self):
        result = _classify_insolvency(
            "Jordan GmbH",
            "",
            [
                {
                    "title": "Jordan GmbH: Insolvenzverfahren eroeffnet",
                    "snippet": "Fuer Jordan GmbH wurde ein vorlaeufiger Insolvenzverwalter bestellt.",
                    "url": "https://example.test/jordan-gmbh",
                }
            ],
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(len(result["evidence"]), 1)

    def test_register_mismatch_rejects_even_with_shared_name_token(self):
        result = _classify_insolvency(
            "Jordan GmbH",
            "",
            [
                {
                    "title": "Jordan Capital GmbH - Insolvenz",
                    "snippet": "Jordan Capital GmbH, HRB 9999, Amtsgericht Moenchengladbach, Az. 2 IN 277/26.",
                    "url": "https://example.test/jordan-capital",
                }
            ],
            hr_no="HRB 2537",
            register_court="Braunschweig",
        )

        self.assertFalse(result["insolvenzverfahren"])
        self.assertEqual(result["evidence"], [])

    def test_register_match_accepts_page_text_identity(self):
        result = _classify_insolvency(
            "Jordan GmbH",
            "",
            [
                {
                    "title": "Insolvenzbekanntmachung",
                    "snippet": "Az. 2 IN 277/26. Insolvenzverfahren eroeffnet.",
                    "_page_text": "Jordan GmbH, HRB 2537, Amtsgericht Braunschweig.",
                    "url": "https://example.test/jordan-register",
                }
            ],
            hr_no="HRB 2537",
            register_court="Braunschweig",
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(len(result["evidence"]), 1)

    def test_plus_sign_legal_name_triggers_for_nill_ritz(self):
        result = _classify_insolvency(
            "Nill + Ritz CNC-Technik GmbH",
            "",
            [
                {
                    "title": "Nill + Ritz CNC Technik GmbH",
                    "snippet": (
                        "Insolvenzverfahren Nill + Ritz CNC Technik GmbH, "
                        "Maulbronner Weg 38, 71706 Markgroeningen."
                    ),
                    "url": "https://example.test/nill-ritz",
                },
                {
                    "title": "Nill + Ritz CNC-Technik GmbH, Markgroeningen",
                    "snippet": (
                        "Durch Beschluss des Amtsgerichts Ludwigsburg vom 02.04.2026 "
                        "(2 IN 277/26) wurde ein vorlaeufiger Insolvenzverwalter bestellt."
                    ),
                    "url": "https://example.test/northdata",
                },
            ],
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(len(result["evidence"]), 2)

    def test_northdata_registry_page_does_not_raise_suspicion(self):
        # North Data is a registry directory: every profile carries an
        # "Insolvenzverfahren" section label. A match there is structural noise,
        # not a real proceeding, so it must never count as evidence.
        result = _classify_insolvency(
            "MENTOR GmbH & Co. Praezisions-Bauteile KG",
            "",
            [
                {
                    "title": "MENTOR GmbH & Co. Praezisions-Bauteile KG - North Data",
                    "snippet": "Insolvenzverfahren Bilanzsumme Umsatz Mitarbeiter Erkrath",
                    "url": "https://www.northdata.de/MENTOR+GmbH",
                }
            ],
        )

        self.assertFalse(result["insolvenzverfahren"])
        self.assertEqual(result["evidence"], [])

    def test_real_proceeding_still_triggers_from_non_registry_domain(self):
        # Same keyword, real court signal, non-registry source -> must still fire.
        result = _classify_insolvency(
            "Elektrotechnik Jordan GmbH",
            "",
            [
                {
                    "title": "Elektrotechnik Jordan GmbH: Insolvenzverfahren eroeffnet",
                    "snippet": (
                        "Fuer Elektrotechnik Jordan GmbH wurde ein vorlaeufiger "
                        "Insolvenzverwalter bestellt, Az. 2 IN 277/26."
                    ),
                    "url": "https://insolvenzbekanntmachungen.de/jordan",
                }
            ],
        )

        self.assertTrue(result["insolvenzverfahren"])
        self.assertEqual(len(result["evidence"]), 1)


if __name__ == "__main__":
    unittest.main()
