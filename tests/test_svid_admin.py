import json

import pytest

from eap_middleware.profiles import ProfileRegistry
from eap_middleware.svid_admin import SvidAdminConfig, SvidAdminError


def test_svid_admin_accepts_name_and_engineering_formats(tmp_path):
    profile = ProfileRegistry().get("spts_fxp_omega")
    (tmp_path / "DataCollectSwitch.json").write_text(
        json.dumps({"DataCollectSwitch": "ON", "DataIntervalInSec": 2}),
        encoding="utf-8",
    )
    (tmp_path / "RecipeList.json").write_text(
        json.dumps({"Recipe_List": ["RCP1"]}),
        encoding="utf-8",
    )
    (tmp_path / "SvidList.json").write_text(
        json.dumps(
            {
                "RecipeSvidList": ["RCP1"],
                "SvidList": [
                    "MDLN",
                    {"SVID": 28, "Name": "ControlState"},
                    "DOES_NOT_EXIST",
                ],
            }
        ),
        encoding="utf-8",
    )

    state = SvidAdminConfig(tmp_path, profile).load()

    assert state.enabled is True
    assert state.interval_sec == 2
    assert state.recipe_list == ["RCP1"]
    assert [item.svid for item in state.svids] == [32, 28]
    assert state.invalid_entries == ["DOES_NOT_EXIST"]


def test_svid_admin_off_stops_collection(tmp_path):
    profile = ProfileRegistry().get("spts_fxp_omega")
    (tmp_path / "DataCollectSwitch.json").write_text(
        json.dumps({"DataCollectSwitch": "OFF", "DataIntervalInSec": 1}),
        encoding="utf-8",
    )
    state = SvidAdminConfig(tmp_path, profile).load()
    assert state.enabled is False


def test_svid_admin_rejects_bad_interval(tmp_path):
    profile = ProfileRegistry().get("spts_fxp_omega")
    (tmp_path / "DataCollectSwitch.json").write_text(
        json.dumps({"DataCollectSwitch": "ON", "DataIntervalInSec": 0}),
        encoding="utf-8",
    )
    with pytest.raises(SvidAdminError):
        SvidAdminConfig(tmp_path, profile).load()

