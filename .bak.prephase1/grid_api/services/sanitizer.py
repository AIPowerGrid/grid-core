# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prompt sanitizer — strips credentials and secrets before they reach workers.

Only catches high-confidence patterns with unique prefixes that are almost
never false positives. Replaces with model-friendly text so the LLM knows
what happened and can respond appropriately.

Runs in microseconds — compiled regex, no network calls, no ML.
"""

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger("grid_api.sanitizer")


@dataclass
class RedactionResult:
    text: str
    redacted: bool
    count: int
    types: list[str]


# ── Patterns ──
# Each tuple: (compiled_regex, replacement_text, type_label)
# Only patterns with near-zero false positive rates.

_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _p(pattern: str, replacement: str, label: str, flags: int = 0):
    """Register a pattern."""
    _PATTERNS.append((re.compile(pattern, flags), replacement, label))


# AWS Access Key ID — always starts with AKIA, exactly 20 uppercase alphanumeric
_p(
    r'AKIA[0-9A-Z]{16}',
    '[REDACTED: AWS access key removed for your safety — never share credentials with AI]',
    'aws_access_key',
)

# AWS Secret Access Key — 40 chars, typically appears near "secret"
_p(
    r'(?i)(?:aws[_\s-]*)?secret[_\s-]*(?:access)?[_\s-]*key[\s:="\']+'
    r'([A-Za-z0-9/+=]{40})',
    '[REDACTED: AWS secret key removed for your safety]',
    'aws_secret_key',
)

# PEM private keys (RSA, EC, OPENSSH, DSA, generic)
_p(
    r'-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|DSA\s+)?PRIVATE\s+KEY-----'
    r'[\s\S]*?'
    r'-----END\s+(?:RSA\s+|EC\s+|OPENSSH\s+|DSA\s+)?PRIVATE\s+KEY-----',
    '[REDACTED: private key removed for your safety — never share private keys with AI]',
    'private_key',
    re.DOTALL,
)

# PEM certificates (less sensitive but still shouldn't be forwarded)
_p(
    r'-----BEGIN\s+CERTIFICATE-----'
    r'[\s\S]*?'
    r'-----END\s+CERTIFICATE-----',
    '[REDACTED: certificate removed]',
    'certificate',
    re.DOTALL,
)

# OpenAI API keys — sk-[proj-]...
_p(
    r'sk-(?:proj-)?[A-Za-z0-9_-]{20,}',
    '[REDACTED: OpenAI API key removed for your safety]',
    'openai_key',
)

# Anthropic API keys — sk-ant-...
_p(
    r'sk-ant-[A-Za-z0-9_-]{20,}',
    '[REDACTED: Anthropic API key removed for your safety]',
    'anthropic_key',
)

# GitHub tokens — ghp_, gho_, ghs_, ghr_, github_pat_
_p(
    r'(?:ghp|gho|ghs|ghr)_[A-Za-z0-9_]{30,}',
    '[REDACTED: GitHub token removed for your safety]',
    'github_token',
)
_p(
    r'github_pat_[A-Za-z0-9_]{20,}',
    '[REDACTED: GitHub personal access token removed for your safety]',
    'github_pat',
)

# GitLab tokens — glpat-...
_p(
    r'glpat-[A-Za-z0-9_-]{20,}',
    '[REDACTED: GitLab token removed for your safety]',
    'gitlab_token',
)

# Slack tokens — xoxb-, xoxp-, xoxa-, xoxo-, xoxs-
_p(
    r'xox[bpaso]-[A-Za-z0-9-]{10,}',
    '[REDACTED: Slack token removed for your safety]',
    'slack_token',
)

# Stripe keys — sk_live_, sk_test_, pk_live_, pk_test_, rk_live_, rk_test_
_p(
    r'(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}',
    '[REDACTED: Stripe key removed for your safety]',
    'stripe_key',
)

# Twilio — starts with SK followed by 32 hex chars
_p(
    r'SK[0-9a-fA-F]{32}',
    '[REDACTED: Twilio key removed for your safety]',
    'twilio_key',
)

# SendGrid — SG. followed by base64
_p(
    r'SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}',
    '[REDACTED: SendGrid key removed for your safety]',
    'sendgrid_key',
)

# Mailgun — key-...
_p(
    r'key-[A-Za-z0-9]{32,}',
    '[REDACTED: Mailgun key removed for your safety]',
    'mailgun_key',
)

# Database connection strings — proto://user:password@host
_p(
    r'(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)'
    r'://[^\s:]+:[^\s@]+@[^\s]+',
    '[REDACTED: database connection string removed for your safety — never share credentials with AI]',
    'connection_string',
)

# Discord bot tokens — typically base64-ish with dots
_p(
    r'(?:Bot\s+|Bearer\s+)?[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}',
    '[REDACTED: Discord token removed for your safety]',
    'discord_token',
)

# Ethereum/crypto private keys — 64 hex chars preceded by context words
_p(
    r'(?i)(?:private[_\s-]*key|priv[_\s-]*key|secret[_\s-]*key)[\s:="\']+(?:0x)?([0-9a-fA-F]{64})',
    '[REDACTED: crypto private key removed for your safety — never share private keys with AI]',
    'crypto_private_key',
)

# Bitcoin WIF private keys — start with 5, K, or L, 51-52 chars base58
_p(
    r'(?i)(?:private[_\s-]*key|wif|secret)[\s:="\']+([5KL][1-9A-HJ-NP-Za-km-z]{50,51})',
    '[REDACTED: Bitcoin private key removed for your safety]',
    'btc_private_key',
)

# Seed phrases / mnemonics — 12 or 24 lowercase words (common BIP39 pattern)
# Only match when preceded by context words to avoid false positives
_p(
    r'(?i)(?:seed\s*phrase|mnemonic|recovery\s*phrase|backup\s*phrase)[\s:="\']+'
    r'((?:[a-z]+\s+){11,23}[a-z]+)',
    '[REDACTED: seed phrase removed for your safety — never share your recovery phrase with AI]',
    'seed_phrase',
)

# Google API keys — AIza followed by 35 chars
_p(
    r'AIza[0-9A-Za-z_-]{35}',
    '[REDACTED: Google API key removed for your safety]',
    'google_api_key',
)

# Vercel tokens
_p(
    r'vercel_[A-Za-z0-9_-]{20,}',
    '[REDACTED: Vercel token removed for your safety]',
    'vercel_token',
)

# Supabase keys
_p(
    r'sbp_[A-Za-z0-9]{30,}',
    '[REDACTED: Supabase key removed for your safety]',
    'supabase_key',
)

# npm tokens
_p(
    r'npm_[A-Za-z0-9]{30,}',
    '[REDACTED: npm token removed for your safety]',
    'npm_token',
)

# Heroku API key — 36-char UUID format preceded by context
_p(
    r'(?i)heroku[\s_-]*(?:api)?[\s_-]*key[\s:="\']+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    '[REDACTED: Heroku API key removed for your safety]',
    'heroku_key',
)

# ── From h33tlit/secret-regex-list — additional high-confidence patterns ──

# Amazon MWS Auth Token
_p(
    r'amzn\.mws\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    '[REDACTED: Amazon MWS token removed for your safety]',
    'amazon_mws_token',
)

# Facebook Access Token
_p(
    r'EAACEdEose0cBA[0-9A-Za-z]+',
    '[REDACTED: Facebook access token removed for your safety]',
    'facebook_token',
)

# Google OAuth Access Token — ya29.
_p(
    r'ya29\.[0-9A-Za-z_-]+',
    '[REDACTED: Google OAuth token removed for your safety]',
    'google_oauth_token',
)

# Google Cloud Service Account JSON
_p(
    r'"type"\s*:\s*"service_account"',
    '[REDACTED: Google service account config detected — do not share credentials with AI]',
    'gcp_service_account',
)

# PGP private key
_p(
    r'-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----',
    '[REDACTED: PGP private key removed for your safety]',
    'pgp_private_key',
    re.DOTALL,
)

# Slack Webhook URL
_p(
    r'https://hooks\.slack\.com/services/T[a-zA-Z0-9_]{8,}/B[a-zA-Z0-9_]{8,}/[a-zA-Z0-9_]{24,}',
    '[REDACTED: Slack webhook URL removed for your safety]',
    'slack_webhook',
)

# Square Access Token
_p(
    r'sq0atp-[0-9A-Za-z_-]{22,}',
    '[REDACTED: Square access token removed for your safety]',
    'square_token',
)

# Square OAuth Secret
_p(
    r'sq0csp-[0-9A-Za-z_-]{40,}',
    '[REDACTED: Square OAuth secret removed for your safety]',
    'square_oauth',
)

# PayPal Braintree Access Token
_p(
    r'access_token\$production\$[0-9a-z]{16}\$[0-9a-f]{32}',
    '[REDACTED: PayPal/Braintree token removed for your safety]',
    'paypal_token',
)

# MailChimp API Key
_p(
    r'[0-9a-f]{32}-us[0-9]{1,2}',
    '[REDACTED: MailChimp API key removed for your safety]',
    'mailchimp_key',
)

# Picatic API Key (same prefix as Stripe live)
_p(
    r'sk_live_[0-9a-z]{32}',
    '[REDACTED: API key removed for your safety]',
    'picatic_key',
)

# Cloudinary URL
_p(
    r'cloudinary://[^\s]+',
    '[REDACTED: Cloudinary URL with credentials removed for your safety]',
    'cloudinary_url',
)

# Password in URL (generic) — proto://user:pass@host
_p(
    r'[a-zA-Z]{3,10}://[^\s/:@]{3,20}:[^\s/:@]{3,20}@[^\s"\']+',
    '[REDACTED: URL with embedded credentials removed for your safety]',
    'password_in_url',
)


def sanitize(text: str) -> RedactionResult:
    """Sanitize a string by replacing detected credentials with safe placeholders.

    Returns a RedactionResult with the cleaned text, whether anything was
    redacted, the count, and the types of secrets found.
    """
    if not text:
        return RedactionResult(text=text, redacted=False, count=0, types=[])

    total = 0
    types = []
    result = text

    for pattern, replacement, label in _PATTERNS:
        new_result, count = pattern.subn(replacement, result)
        if count > 0:
            total += count
            types.append(label)
            result = new_result

    if total > 0:
        logger.warning(f"Sanitized {total} secret(s) from prompt: {', '.join(types)}")

    return RedactionResult(text=result, redacted=total > 0, count=total, types=types)


def sanitize_messages(messages: list[dict]) -> tuple[list[dict], bool, list[str]]:
    """Sanitize all message content in an OpenAI-format messages array.

    Returns (sanitized_messages, was_redacted, types_found).
    """
    all_types = []
    was_redacted = False
    sanitized = []

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            result = sanitize(content)
            sanitized.append({**msg, "content": result.text})
            if result.redacted:
                was_redacted = True
                all_types.extend(result.types)
        else:
            sanitized.append(msg)

    return sanitized, was_redacted, all_types
