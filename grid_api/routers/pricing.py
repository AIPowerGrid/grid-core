# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public, read-only Grid pricing evidence."""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from ..services import pricing as pricing_service

router = APIRouter()


class PublicModelRates(BaseModel):
    input_per_mtok_usd: float | None
    output_per_mtok_usd: float | None
    per_image_usd: float | None
    per_video_second_usd: float | None
    per_audio_second_usd: float | None
    per_3d_generation_usd: float | None


class PublicModelPrice(BaseModel):
    model: str
    rates: PublicModelRates


class PublicPriceBook(BaseModel):
    version: str
    availability: str
    models: list[PublicModelPrice]
    aliases: dict[str, str]


class PublicPriceComparison(BaseModel):
    id: str
    model: str
    modality: Literal["text", "image"]
    provider: str
    source_url: str
    basis: str
    workload: dict[str, int]
    competitor_rates: dict[str, float]
    aipg_usd: float
    competitor_usd: float
    savings_percent: float


class PublicComparisonEvidence(BaseModel):
    status: Literal["not_yet_valid", "current", "stale"]
    as_of: str
    valid_until: str
    items: list[PublicPriceComparison]
    scope: str


class PublicPricingCatalog(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["aipg.pricing.v1"] = Field(alias="schema")
    currency: Literal["USD"]
    ledger_unit: Literal["micro_usd"]
    price_book: PublicPriceBook
    comparison_evidence: PublicComparisonEvidence


@router.get("/v1/pricing", response_model=PublicPricingCatalog)
async def pricing_catalog(response: Response) -> dict:
    response.headers["Cache-Control"] = "public, max-age=300"
    return pricing_service.public_catalog()
