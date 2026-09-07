# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from grid_api.config import GridSettings
from grid_api.services import validator_operators as operators


@pytest.mark.parametrize(
    "upgrade,upgrades",
    [("", []), ("v0.1.0-preview.14", []), ("", ["v0.1.0-preview.15"]),
     ("", ["v0.1.0-preview.15", "v0.1.0-preview.16"]),
     ("", ["v0.1.0-preview.15", "v0.1.0-preview.16", "v0.1.0-preview.17"])],
)
def test_upgrade_python_and_sql_eligibility_agree(monkeypatch, upgrade, upgrades):
    monkeypatch.setattr(
        operators,
        "get_settings",
        lambda: SimpleNamespace(
            validator_cohort_baseline_version="v0.1.0-preview.13",
            validator_cohort_upgrade_version=upgrade,
            validator_cohort_upgrade_versions=upgrades,
        ),
    )
    versions = [
        "v0.1.0-preview.13",
        "0.1.0-preview.13",
        "v0.1.0-preview.14",
        "v0.1.0-preview.15",
        "v0.1.0-preview.16",
        "0.1.0-preview.16",
        "v0.1.0-preview.17",
        "0.1.0-preview.17",
        "v0.1.0-preview.18",
        "vv0.1.0-preview.16",
        "v0.1.0-preview.9",
        "v0.1.0-dev",
        "vv0.1.0-preview.13",
        "\tv0.1.0-preview.13\n",
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
                    or any(version in (tag, tag.removeprefix("v")) for tag in upgrades)
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


@pytest.mark.parametrize(
    "versions",
    [
        ["*"],
        ["latest"],
        ["v0.1.0-dev"],
        ["0.1.0-preview.16"],
        [" v0.1.0-preview.16"],
        ["v0.1.0-preview.16\n"],
        ["v0.1.0-preview.16,v0.1.0-preview.15"],
        [""],
        [None],
        ["v0.1.0-preview.16", "v0.1.0-preview.16"],
        ["v0.1.0-preview.14", "v0.1.0-preview.15", "v0.1.0-preview.16", "v0.1.0-preview.17"],
    ],
)
def test_plural_upgrade_rejects_unreviewable_or_unbounded_versions(versions):
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None, validator_cohort_upgrade_versions=versions)


def test_upgrade_configuration_cannot_silently_combine_lists():
    with pytest.raises(ValidationError, match="not both"):
        GridSettings(
            _env_file=None, validator_cohort_upgrade_version="v0.1.0-preview.15", validator_cohort_upgrade_versions=["v0.1.0-preview.16"],
        )


@pytest.mark.parametrize("upgrades", [
    ["v0.1.0-preview.15", "v0.1.0-preview.16"],
    ["v0.1.0-preview.15", "v0.1.0-preview.16", "v0.1.0-preview.17"],
])
def test_plural_upgrade_overlap_cannot_run_shadow_observer(upgrades):
    with pytest.raises(ValidationError, match="overlap requires shadow observation disabled"):
        GridSettings(
            _env_file=None,
            validator_cohort_upgrade_versions=upgrades,
            validator_shadow_observer_enabled=True,
        )


@pytest.mark.parametrize("upgrades", [
    '["v0.1.0-preview.15","v0.1.0-preview.16"]',
    '["v0.1.0-preview.15","v0.1.0-preview.16","v0.1.0-preview.17"]',
])
def test_plural_upgrade_loads_from_json_environment(monkeypatch, upgrades):
    import json

    monkeypatch.setenv("VALIDATOR_COHORT_UPGRADE_VERSION", "")
    monkeypatch.setenv("VALIDATOR_COHORT_UPGRADE_VERSIONS", upgrades)
    settings = GridSettings(_env_file=None)
    assert settings.validator_cohort_upgrade_versions == json.loads(upgrades)
    assert settings.validator_shadow_observer_enabled is False
