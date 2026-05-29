import unittest
from email.message import EmailMessage

from execution.email_parsers import (
    FlatfoxParser,
    HomegateParser,
    ImmoScout24Parser,
    NewhomeParser,
    WGZimmerParser,
    parser_for_sender,
)
from execution.email_parsers.base import (
    BaseEmailParser,
    context_window,
    dedupe_urls,
    extract_message_body,
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


class ParserDispatchTest(unittest.TestCase):
    def test_parser_for_sender_routes_each_portal(self) -> None:
        cases = {
            "Alerts <news@homegate.ch>": HomegateParser,
            "no-reply@wgzimmer.ch": WGZimmerParser,
            "alert@newhome.ch": NewhomeParser,
            "noreply@immoscout24.ch": ImmoScout24Parser,
            "owner.foo@user.flatfox.ch": FlatfoxParser,
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
        self.assertIn("Neue Inserate", parsed[0].raw_text)
        self.assertEqual(parsed[0].title, "Neue Inserate: Wohnung in Luzern CHF 920")


if __name__ == "__main__":
    unittest.main()
