"""build_parser() must assemble a usable config for the "auto" engine: pick
a fallback AI provider (saved choice, else auto-detect by which key is
present, else Gemini) and carry that provider's own key/model into config."""
from database.database import set_setting
from services.ai_invoice_parser import InvoiceParser
from utils.security import encrypt_secret


def test_auto_engine_defaults_to_gemini_with_no_keys_saved(session, company, user):
    from app_pages.upload_bill import build_parser

    set_setting(session, company.id, "ocr_provider", "auto")
    parser, name = build_parser(session, company)
    assert name == "auto"
    assert isinstance(parser, InvoiceParser)
    assert parser.provider.name == "auto"
    assert parser.provider._fallback_name == "gemini"


def test_auto_engine_auto_detects_the_configured_key(session, company, user):
    from app_pages.upload_bill import build_parser

    set_setting(session, company.id, "ocr_provider", "auto")
    set_setting(session, company.id, "openai_api_key", encrypt_secret("sk-test-123"), True)
    parser, _ = build_parser(session, company)
    assert parser.provider._fallback_name == "openai"
    assert parser.provider.config["api_key"] == "sk-test-123"


def test_auto_engine_prefers_the_explicitly_saved_fallback_choice(session, company, user):
    from app_pages.upload_bill import build_parser

    set_setting(session, company.id, "ocr_provider", "auto")
    set_setting(session, company.id, "openai_api_key", encrypt_secret("sk-openai"), True)
    set_setting(session, company.id, "anthropic_api_key", encrypt_secret("sk-claude"), True)
    set_setting(session, company.id, "auto_fallback_provider", "claude")
    parser, _ = build_parser(session, company)
    assert parser.provider._fallback_name == "claude"
    assert parser.provider.config["api_key"] == "sk-claude"


def test_auto_engine_carries_the_fallback_models_own_saved_model(session, company, user):
    from app_pages.upload_bill import build_parser

    set_setting(session, company.id, "ocr_provider", "auto")
    set_setting(session, company.id, "gemini_api_key", encrypt_secret("sk-gem"), True)
    set_setting(session, company.id, "gemini_model", "gemini-flash-latest")
    parser, _ = build_parser(session, company)
    assert parser.provider.config["model"] == "gemini-flash-latest"
