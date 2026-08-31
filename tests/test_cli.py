import json

from eap_middleware.cli import main


def test_list_profiles_outputs_configurable_machine_profiles(capsys):
    exit_code = main(["list-profiles", "--json"])

    output = json.loads(capsys.readouterr().out)
    profile_ids = {item["machine_profile"] for item in output}

    assert exit_code == 0
    assert {
        "spts_fxp_omega",
        "davinci_200_mc4_hc1",
        "ptiq_secsgem",
    }.issubset(profile_ids)
    spts = next(item for item in output if item["machine_profile"] == "spts_fxp_omega")
    assert spts["vendor"] == "SPTS"
    assert spts["svids"] > 0
