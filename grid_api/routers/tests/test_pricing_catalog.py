# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Response

from grid_api.routers import pricing as pricing_router
from grid_api.services import pricing


def test_public_catalog_exposes_exact_rates_and_narrow_current_comparisons():
    catalog = pricing.public_catalog(datetime(2026, 8, 29, 12, tzinfo=UTC))

    assert catalog["schema"] == "aipg.pricing.v1"
    assert catalog["price_book"]["version"] == "2026-08-29-a"
    assert catalog["comparison_evidence"]["status"] == "current"
    comparisons = {
        item["id"]: item for item in catalog["comparison_evidence"]["items"]
    }

    text = comparisons["gpt-oss-120b-standard-token-rates"]
    assert text["aipg_usd"] == 0.375
    assert text["competitor_usd"] == 0.75
    assert text["savings_percent"] == 50.0
    assert text["source_url"].startswith("https://console.groq.com/")

    image = comparisons["z-image-turbo-one-megapixel"]
    assert image["aipg_usd"] == 0.003
    assert image["competitor_usd"] == 0.005
    assert image["savings_percent"] == 40.0
    assert image["source_url"].startswith("https://fal.ai/")


def test_public_catalog_omits_stale_comparison_claims_but_keeps_grid_rates():
    catalog = pricing.public_catalog(datetime(2026, 10, 1, tzinfo=UTC))

    assert catalog["comparison_evidence"]["status"] == "stale"
    assert catalog["comparison_evidence"]["items"] == []
    assert catalog["price_book"]["models"]


def test_public_catalog_does_not_publish_comparisons_before_the_review_epoch():
    catalog = pricing.public_catalog(datetime(2026, 8, 28, tzinfo=UTC))

    assert catalog["comparison_evidence"]["status"] == "not_yet_valid"
    assert catalog["comparison_evidence"]["items"] == []


def test_public_catalog_rejects_naive_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        pricing.public_catalog(datetime(2026, 8, 29))


@pytest.mark.asyncio
async def test_public_pricing_route_is_cacheable_and_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        pricing_router.pricing_service,
        "public_catalog",
        lambda: {"schema": "aipg.pricing.v1"},
    )
    response = Response()

    result = await pricing_router.pricing_catalog(response)

    assert result == {"schema": "aipg.pricing.v1"}
    assert response.headers["cache-control"] == "public, max-age=300"


def test_public_pricing_route_has_a_typed_openapi_contract():
    app = FastAPI()
    app.include_router(pricing_router.router)

    response_schema = app.openapi()["paths"]["/v1/pricing"]["get"]["responses"]["200"]

    assert response_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/PublicPricingCatalog",
    )


def test_public_pricing_response_preserves_the_wire_schema_name():
    catalog = pricing.public_catalog(datetime(2026, 8, 29, 12, tzinfo=UTC))

    validated = pricing_router.PublicPricingCatalog.model_validate(catalog)
    wire = validated.model_dump(by_alias=True)

    assert wire["schema"] == "aipg.pricing.v1"
    assert "schema_version" not in wire
