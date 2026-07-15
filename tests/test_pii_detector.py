"""Unit tests for governance/pii_detector.py — no DB, no LLM needed."""
import pytest
from governance.pii_detector import find_emails, find_phones, scan_dict_for_pii, PIIHit


class TestFindEmails:
    def test_finds_standard_email(self):
        assert find_emails("contact alice@example.com") == ["alice@example.com"]

    def test_finds_multiple_emails(self):
        result = find_emails("alice@example.com and bob@example.com")
        assert "alice@example.com" in result
        assert "bob@example.com" in result

    def test_clean_text_has_no_emails(self):
        assert find_emails("I want a refund for order ORD-001") == []

    def test_incomplete_email_not_matched(self):
        assert find_emails("contact@") == []
        assert find_emails("@example.com") == []


class TestFindPhones:
    def test_finds_e164_phone(self):
        result = find_phones("+1-555-100-0001")
        assert len(result) > 0

    def test_finds_dotted_phone(self):
        result = find_phones("555.100.0001")
        assert len(result) > 0

    def test_clean_text_has_no_phones(self):
        assert find_phones("My order arrived broken.") == []

    def test_short_number_not_matched(self):
        # 4-digit numbers should not match
        assert find_phones("item 1234") == []


class TestScanDictForPII:
    def test_finds_email_in_flat_dict(self):
        data = {"contact_email": "bob@example.com", "order_id": "ORD-001"}
        hits = scan_dict_for_pii(data)
        assert any(h.pii_type == "email" and h.value == "bob@example.com" for h in hits)

    def test_clean_dict_returns_no_hits(self):
        data = {
            "order_id": "ORD-001",
            "product_type": "Electronics",
            "amount_paid": 299.99,
        }
        assert scan_dict_for_pii(data) == []

    def test_finds_email_in_nested_dict(self):
        data = {"customer": {"email": "alice@example.com"}}
        hits = scan_dict_for_pii(data)
        assert any(h.pii_type == "email" for h in hits)

    def test_field_path_is_dot_separated(self):
        data = {"contact_email": "bob@example.com"}
        hits = scan_dict_for_pii(data)
        assert hits[0].field == "contact_email"

    def test_nested_field_path_is_correct(self):
        data = {"order": {"customer": {"email": "alice@example.com"}}}
        hits = scan_dict_for_pii(data)
        assert hits[0].field == "order.customer.email"

    def test_finds_phone_in_dict(self):
        data = {"contact_phone": "+1-555-100-0002"}
        hits = scan_dict_for_pii(data)
        assert any(h.pii_type == "phone" for h in hits)

    def test_returns_pii_hit_dataclass(self):
        data = {"contact_email": "alice@example.com"}
        hits = scan_dict_for_pii(data)
        assert isinstance(hits[0], PIIHit)
        assert hits[0].pii_type == "email"
        assert hits[0].value == "alice@example.com"
