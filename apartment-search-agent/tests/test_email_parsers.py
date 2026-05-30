import unittest
from email.message import EmailMessage

from execution.email_parsers import (
    AnibisParser,
    ComparisParser,
    FlatfoxParser,
    HomegateParser,
    ImmoScout24Parser,
    NewhomeParser,
    RonorpParser,
    TuttiParser,
    WGZimmerParser,
    parser_for_sender,
)
from execution.email_parsers.base import (
    BaseEmailParser,
    context_window,
    dedupe_urls,
    extract_anchor_text,
    extract_message_body,
    extract_rent_chf,
    html_to_text,
)


def make_message(
    *,
    sender: str,
    subject: str = "Test alert",
    plain: str = "",
    html: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = f"<fixture-{abs(hash((sender, subject)))}@test>"
    if plain and html:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content("", subtype="plain")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(plain or "")
    return msg


class HelperTest(unittest.TestCase):
    def test_html_to_text_strips_tags_and_decodes_entities(self) -> None:
        html = "<p>Zimmer in <b>Luzern</b> &ndash; CHF 800</p>"
        self.assertIn("Zimmer in Luzern", html_to_text(html))
        self.assertIn("CHF 800", html_to_text(html))

    def test_html_to_text_drops_script_and_style(self) -> None:
        html = "<style>.x{}</style><script>alert(1)</script><div>visible</div>"
        out = html_to_text(html)
        self.assertNotIn("alert", out)
        self.assertNotIn(".x{}", out)
        self.assertIn("visible", out)

    def test_dedupe_urls_preserves_order_and_trims_punctuation(self) -> None:
        urls = ["https://a.test/x).", "https://a.test/x", "https://b.test/y"]
        self.assertEqual(dedupe_urls(urls), ["https://a.test/x", "https://b.test/y"])

    def test_context_window_returns_text_around_url(self) -> None:
        url = "https://example.test/listing"
        body = ("lorem " * 50) + url + (" ipsum" * 50)
        window = context_window(body, url, radius=40)
        self.assertIn(url, window)
        self.assertLessEqual(len(window), 200)

    def test_extract_message_body_returns_plain_and_html(self) -> None:
        msg = make_message(
            sender="test@homegate.ch",
            plain="plaintext body",
            html="<p>html body</p>",
        )
        plain, html = extract_message_body(msg)
        self.assertIn("plaintext body", plain)
        self.assertIn("<p>html body</p>", html)


class StructuredExtractionHelperTest(unittest.TestCase):
    def test_extract_anchor_text_returns_visible_text(self) -> None:
        html = "<a href='https://example.test/x'>WG in Buchrain CHF 750</a>"
        self.assertEqual(
            extract_anchor_text(html, "https://example.test/x"),
            "WG in Buchrain CHF 750",
        )

    def test_extract_anchor_text_returns_none_when_url_absent(self) -> None:
        self.assertIsNone(extract_anchor_text("<a href='other'>x</a>", "missing"))
        self.assertIsNone(extract_anchor_text("", "any"))

    def test_extract_rent_chf_parses_chf_prefix(self) -> None:
        self.assertEqual(extract_rent_chf("CHF 850"), 850)
        self.assertEqual(extract_rent_chf("CHF 1'250"), 1250)
        self.assertEqual(extract_rent_chf("Fr. 720"), 720)
        self.assertEqual(extract_rent_chf("720 CHF"), 720)

    def test_extract_rent_chf_skips_implausible_amounts(self) -> None:
        # Numbers under 100 (e.g. apartment number "12") and over 10_000
        # (annual/deposit lump) shouldn't be picked.
        self.assertIsNone(extract_rent_chf("Wohnung Nr. 12 CHF"))
        self.assertIsNone(extract_rent_chf("CHF 25000 Jahresmiete"))

    def test_extract_rent_chf_handles_empty(self) -> None:
        self.assertIsNone(extract_rent_chf(""))
        self.assertIsNone(extract_rent_chf("no money mentioned"))

    def test_extract_rent_chf_handles_decimals_and_grouping(self) -> None:
        # Reviewer H1: explicit cents must not push the amount past the
        # plausibility ceiling (85000) and get dropped.
        self.assertEqual(extract_rent_chf("CHF 850.00"), 850)
        self.assertEqual(extract_rent_chf("CHF 850,50"), 850)
        self.assertEqual(extract_rent_chf("CHF 1'250.00"), 1250)
        self.assertEqual(extract_rent_chf("Miete Fr. 1'250.– pro Monat"), 1250)
        self.assertEqual(extract_rent_chf("950.- CHF"), 950)

    def test_extract_rent_chf_ignores_dates_and_phone_numbers(self) -> None:
        # Reviewer H2: dates and grouped phone numbers must not surface a rent.
        self.assertIsNone(extract_rent_chf("Frei ab 1.7.2026"))
        self.assertIsNone(extract_rent_chf("Tel. 079 123 4567 CHF"))
        self.assertIsNone(extract_rent_chf("Kontakt unter +41 79 123 45 67"))

    def test_extract_anchor_text_handles_unquoted_and_tracking_query(self) -> None:
        # Reviewer H3: unquoted hrefs and appended tracking params must still
        # resolve to the anchor's visible text.
        url = "https://example.test/listing/123"
        self.assertEqual(
            extract_anchor_text(f"<a href={url}>WG in Root</a>", url),
            "WG in Root",
        )
        self.assertEqual(
            extract_anchor_text(
                f"<a href='{url}?utm_source=alert&id=9'>WG in Ebikon CHF 800</a>", url
            ),
            "WG in Ebikon CHF 800",
        )


class ParserDispatchTest(unittest.TestCase):
    def test_parser_for_sender_routes_each_portal(self) -> None:
        cases = {
            "Alerts <news@homegate.ch>": HomegateParser,
            "no-reply@wgzimmer.ch": WGZimmerParser,
            "alert@newhome.ch": NewhomeParser,
            "noreply@immoscout24.ch": ImmoScout24Parser,
            "owner.foo@user.flatfox.ch": FlatfoxParser,
            "noreply@comparis.ch": ComparisParser,
            "alerts@anibis.ch": AnibisParser,
            "no-reply@tutti.ch": TuttiParser,
            "info@ronorp.net": RonorpParser,
        }
        for from_header, expected_cls in cases.items():
            with self.subTest(from_header=from_header):
                parser = parser_for_sender(from_header)
                self.assertIsInstance(parser, expected_cls)

    def test_parser_for_sender_returns_none_for_unknown(self) -> None:
        self.assertIsNone(parser_for_sender("someone@random.example.com"))
        self.assertIsNone(parser_for_sender(""))


class _UrlExtractionMixin:
    parser: BaseEmailParser
    listing_url: str
    sender: str

    def test_extracts_listing_url_from_html(self) -> None:
        html = (
            f"<a href='{self.listing_url}'>WG Zimmer in Luzern, CHF 750</a>"
            "<a href='https://other.test/'>unrelated</a>"
        )
        msg = make_message(sender=self.sender, html=html)
        parsed = self.parser.parse(msg)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].source, self.parser.source)
        self.assertIn(self.listing_url, parsed[0].url)

    def test_extracts_listing_url_from_plain_text(self) -> None:
        plain = (
            f"Neues Inserat: {self.listing_url}\n"
            "CHF 800, Zimmer in Luzern, ab sofort"
        )
        msg = make_message(sender=self.sender, plain=plain)
        parsed = self.parser.parse(msg)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].source, self.parser.source)

    def test_returns_empty_when_no_listing_url(self) -> None:
        msg = make_message(
            sender=self.sender,
            plain="this email mentions nothing useful https://example.com/foo",
        )
        parsed = self.parser.parse(msg)
        self.assertEqual(parsed, [])

    def test_dedupes_repeated_urls(self) -> None:
        html = (
            f"<a href='{self.listing_url}'>link1</a>"
            f"<a href='{self.listing_url}'>link2</a>"
        )
        msg = make_message(sender=self.sender, html=html)
        parsed = self.parser.parse(msg)
        self.assertEqual(len(parsed), 1)


class WGZimmerParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = WGZimmerParser()
    sender = "no-reply@wgzimmer.ch"
    listing_url = "https://www.wgzimmer.ch/wgzimmer/search/mate/ad/abc123.html"


class HomegateParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = HomegateParser()
    sender = "news@homegate.ch"
    listing_url = "https://www.homegate.ch/rent/4001234567"


class NewhomeParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = NewhomeParser()
    sender = "noreply@newhome.ch"
    listing_url = "https://www.newhome.ch/de/mieten/suchen/wohnung/id-12345678"


class ImmoScout24ParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = ImmoScout24Parser()
    sender = "noreply@immoscout24.ch"
    listing_url = "https://www.immoscout24.ch/Mietobjekt/9988776"


class FlatfoxParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = FlatfoxParser()
    sender = "alerts@flatfox.ch"
    listing_url = "https://flatfox.ch/en/flat/luzern/abc-def/"


class ComparisParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = ComparisParser()
    sender = "noreply@comparis.ch"
    listing_url = "https://www.comparis.ch/immobilien/marktplatz/details/show/123456789"


class AnibisParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = AnibisParser()
    sender = "alerts@anibis.ch"
    listing_url = "https://www.anibis.ch/de/d/wohnung-mieten-luzern-3-zimmer-44556677"


class TuttiParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = TuttiParser()
    sender = "no-reply@tutti.ch"
    listing_url = "https://www.tutti.ch/de/a/55667788"


class RonorpParserTest(_UrlExtractionMixin, unittest.TestCase):
    parser = RonorpParser()
    sender = "info@ronorp.net"
    listing_url = "https://www.ronorp.net/zentralschweiz/ronorp/inserate/wohnen/wg-zimmer-luzern-998877"


class FooterAndSearchPageRejectionTest(unittest.TestCase):
    """Saved-search alerts always include footer / search-page links that
    look almost like listings. The patterns must reject them or every alert
    pollutes the tracker with bogus rows."""

    def test_wgzimmer_rejects_search_page_url(self) -> None:
        msg = make_message(
            sender="no-reply@wgzimmer.ch",
            html="<a href='https://www.wgzimmer.ch/wgzimmer/search/mate'>all results</a>",
        )
        self.assertEqual(WGZimmerParser().parse(msg), [])

    def test_homegate_rejects_landing_pages(self) -> None:
        msg = make_message(
            sender="news@homegate.ch",
            html=(
                "<a href='https://www.homegate.ch/rent'>view all results</a>"
                "<a href='https://www.homegate.ch/mieten/luzern'>category</a>"
            ),
        )
        self.assertEqual(HomegateParser().parse(msg), [])

    def test_newhome_rejects_search_results_url(self) -> None:
        msg = make_message(
            sender="noreply@newhome.ch",
            html=(
                "<a href='https://www.newhome.ch/de/mieten/suchen/wohnung/kanton_luzern'>"
                "search</a>"
            ),
        )
        self.assertEqual(NewhomeParser().parse(msg), [])

    def test_immoscout24_rejects_search_results_url(self) -> None:
        msg = make_message(
            sender="noreply@immoscout24.ch",
            html=(
                "<a href='https://www.immoscout24.ch/de/d/wohnung-mieten/luzern'>"
                "all listings</a>"
            ),
        )
        self.assertEqual(ImmoScout24Parser().parse(msg), [])

    def test_comparis_rejects_marketplace_landing(self) -> None:
        msg = make_message(
            sender="noreply@comparis.ch",
            html="<a href='https://www.comparis.ch/immobilien/marktplatz/luzern'>all</a>",
        )
        self.assertEqual(ComparisParser().parse(msg), [])

    def test_anibis_rejects_category_landing(self) -> None:
        msg = make_message(
            sender="alerts@anibis.ch",
            html="<a href='https://www.anibis.ch/de/c/immobilien/wohnung-mieten'>cat</a>",
        )
        self.assertEqual(AnibisParser().parse(msg), [])

    def test_tutti_rejects_category_landing(self) -> None:
        msg = make_message(
            sender="no-reply@tutti.ch",
            html="<a href='https://www.tutti.ch/de/li/luzern/immobilien'>cat</a>",
        )
        self.assertEqual(TuttiParser().parse(msg), [])

    def test_ronorp_rejects_category_landing(self) -> None:
        msg = make_message(
            sender="info@ronorp.net",
            html="<a href='https://www.ronorp.net/zentralschweiz/ronorp/inserate/wohnen'>cat</a>",
        )
        self.assertEqual(RonorpParser().parse(msg), [])

    def test_multiple_footer_urls_do_not_crowd_out_real_listing(self) -> None:
        # Reviewer M6: a real alert is mostly chrome — home, search-results,
        # privacy, app-store and unsubscribe links — wrapped around one real
        # listing. Only the listing (numeric id) may survive.
        real = "https://www.comparis.ch/immobilien/marktplatz/details/show/123456789"
        html = (
            "<a href='https://www.comparis.ch/'>Home</a>"
            "<a href='https://www.comparis.ch/immobilien/marktplatz/luzern'>All results</a>"
            "<a href='https://www.comparis.ch/datenschutz'>Datenschutz</a>"
            "<a href='https://apps.apple.com/app/comparis'>App Store</a>"
            "<a href='https://www.comparis.ch/account/unsubscribe'>Abmelden</a>"
            f"<a href='{real}'>3.5 Zimmer in Luzern, CHF 1’850</a>"
        )
        msg = make_message(sender="noreply@comparis.ch", html=html)
        parsed = ComparisParser().parse(msg)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].url, real)


class RawTextCarriesSubjectTest(unittest.TestCase):
    def test_subject_prefixes_raw_text(self) -> None:
        url = "https://www.homegate.ch/rent/4002233"
        msg = make_message(
            sender="news@homegate.ch",
            subject="Neue Inserate: Wohnung in Luzern CHF 920",
            html=f"<a href='{url}'>open listing</a>",
        )
        parsed = HomegateParser().parse(msg)
        self.assertEqual(len(parsed), 1)
        # Subject still feeds the raw_text the apartment_pipeline scores from.
        self.assertIn("Neue Inserate", parsed[0].raw_text)
        # Structured extraction now prefers anchor text for the title and pulls
        # the rent from the subject/window.
        self.assertEqual(parsed[0].title, "open listing")
        self.assertEqual(parsed[0].rent_chf, 920)

    def test_anchor_text_wins_when_richer_than_subject(self) -> None:
        url = "https://www.homegate.ch/rent/4002244"
        msg = make_message(
            sender="news@homegate.ch",
            subject="Homegate alert",
            html=f"<a href='{url}'>2.5 Zimmer in Buchrain, CHF 1'250</a>",
        )
        parsed = HomegateParser().parse(msg)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].title, "2.5 Zimmer in Buchrain, CHF 1'250")
        self.assertEqual(parsed[0].rent_chf, 1250)


if __name__ == "__main__":
    unittest.main()
