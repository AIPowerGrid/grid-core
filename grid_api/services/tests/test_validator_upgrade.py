# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from grid_api.config import GridSettings
from grid_api.services import validator_operators as operators


@pytest.mark.parametrize("upgrade", ["", "v0.1.0-preview.14"])
def test_upgrade_python_and_sql_eligibility_agree(monkeypatch, upgrade):
    monkeypatch.setattr(
        operators,
        "get_settings",
        lambda: SimpleNamespace(
            validator_cohort_baseline_version="v0.1.0-preview.13",
            validator_cohort_upgrade_version=upgrade,
        ),
    )
    versions = [
        "v0.1.0-preview.13",
        "0.1.0-preview.13",
        "v0.1.0-preview.14",
        "v0.1.0-preview.15",
        "v0.1.0-preview.9",
        "v0.1.0-dev",
        "vv0.1.0-preview.13",
        "",
        None,
    ]
    engine = sa.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            for version in versions:
                baseline, supported = operators.cohort_version_status(version)
                sql_supported = bool(connection.scalar(sa.select(operators.cohort_version_filter(sa.literal(version)))))
                assert sql_supported == supported, version
                assert baseline == "v0.1.0-preview.13"
                assert supported == (
                    version
                    in (
                        "v0.1.0-preview.13",
                        "0.1.0-preview.13",
                    )
                    or bool(upgrade and version == upgrade)
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("version", ["*", "latest", "v0.1.0-dev", "v0.1.0-preview.14,v0.1.0-preview.15"])
def test_upgrade_setting_rejects_unreviewable_versions(version):
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None, validator_cohort_upgrade_version=version)


def test_upgrade_overlap_cannot_run_shadow_observer():
    with pytest.raises(ValidationError, match="overlap requires shadow observation disabled"):
        GridSettings(_env_file=None, validator_cohort_upgrade_version="v0.1.0-preview.14", validator_shadow_observer_enabled=True)
