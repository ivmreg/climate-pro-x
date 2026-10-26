"""Independent evidence replay and malformed-evidence boundaries."""
from copy import deepcopy
from datetime import date, timedelta
from datetime import datetime, timezone
import json

import pytest

from ha_efficiency.evidence import main, replay_hlc_evidence


def bundle():
    rows = []
    for i in range(40):
        x = 5 + i % 10
        rows.append({"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                     "delta_t_k": x, "hlc_adjusted_kwh": 8 * x - 10,
                     "hlc_included": True, "eligible": {"hlc": True}})
    return {"schema_version": 1, "model_version": "1", "daily": rows,
            "configuration": {"boiler_efficiency_used": 0.9},
            "models": {"hlc": {"hlc_w_per_k": 300.0, "r_squared": 1.0,
                                "days_used": 40, "regression_intercept_kwh_per_day": -10}}}


def test_independent_replay_and_temporal_split():
    result = replay_hlc_evidence({"analysis_evidence": bundle()})
    assert result["matches_reported"] is True
    assert result["fit"]["delivered_hlc_w_per_k"] == pytest.approx(300)
    assert result["temporal_holdout"]["training_days"] == 32
    assert result["temporal_holdout"]["rmse_kwh_per_24h"] == pytest.approx(0)


def test_actual_home_assistant_download_wrapper():
    download = {"home_assistant": {"version": "test"},
                "integration_manifest": {"domain": "thermal_efficiency"},
                "data": {"analysis_evidence": bundle()}}
    assert replay_hlc_evidence(download)["matches_reported"] is True


def test_reported_mismatch_is_not_silently_accepted():
    evidence = bundle()
    evidence["models"]["hlc"]["hlc_w_per_k"] = 301
    assert replay_hlc_evidence(evidence)["matches_reported"] is False


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(schema_version=99),
    lambda x: x.update(schema_version=True),
    lambda x: x.update(model_version="future"),
    lambda x: x.update(model_version=True),
    lambda x: x["daily"].append(deepcopy(x["daily"][0])),
    lambda x: x["daily"][0].update(hlc_adjusted_kwh=float("nan")),
    lambda x: x["daily"][0].update(eligible={"hlc": False}),
    lambda x: x["daily"][0].update(eligible=[]),
    lambda x: x["configuration"].update(boiler_efficiency_used=True),
    lambda x: x["configuration"].update(boiler_efficiency_used=10 ** 1000),
    lambda x: x.update(models=[]),
])
def test_malformed_evidence_rejected(mutate):
    evidence = bundle()
    mutate(evidence)
    with pytest.raises(ValueError):
        replay_hlc_evidence(evidence)


def test_excluded_days_do_not_change_the_regression():
    evidence = bundle()
    evidence["daily"].append({"date": "2026-12-01", "hlc_included": False,
                               "eligible": {"hlc": False}, "delta_t_k": None})
    assert replay_hlc_evidence(evidence)["matches_reported"] is True


def test_production_bundle_replays_after_json_roundtrip(thermal_math):
    start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    stats = {key: [] for key in ("room", "outside", "heat", "gas", "water")}
    gas, water = 1000.0, 10000.0
    for hour in range(-1, 100 * 24):
        day = max(hour // 24, 0)
        outside = 18 if day < 35 else 2 + day % 12
        heating = outside < 16
        q = 12 + (7.2 * (20 - outside) if heating else 0)
        ts = start + hour * 3600
        gas += q / 24
        water += 500 / 24
        for key, value in (("room", 20), ("outside", outside), ("heat", 30 if heating else 0)):
            stats[key].append({"start": ts, "mean": value})
        stats["gas"].append({"start": ts, "sum": gas})
        stats["water"].append({"start": ts, "sum": water})
    result = thermal_math.compute_all(stats, {
        "outdoor": "outside", "gas_meter": "gas", "water": "water",
        "boiler_efficiency": 0.88, "rooms": {"bed": {"temperature": "room", "heating_power": "heat"}},
    }, timezone.utc, datetime.fromtimestamp(start + 100 * 24 * 3600, timezone.utc), (120,))
    assert result["hlc"] is not None
    evidence = json.loads(json.dumps(result["analysis_evidence"], allow_nan=False))
    replay = replay_hlc_evidence(evidence)
    assert replay["matches_reported"] is True
    assert replay["fit"]["days_used"] == result["hlc"]["days_used"]
    assert replay["fit"]["delivered_hlc_w_per_k"] == pytest.approx(result["hlc"]["hlc_w_per_k"])


def test_cli_success_mismatch_and_malformed_exit_codes(tmp_path, capsys):
    path = tmp_path / "evidence.json"
    evidence = bundle()
    path.write_text(json.dumps(evidence), encoding="utf-8")
    main([str(path)])
    assert json.loads(capsys.readouterr().out)["matches_reported"] is True
    evidence["models"]["hlc"]["days_used"] = 39
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main([str(path)])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["matches_reported"] is False
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main([str(path)])
    assert exc.value.code == 2
    assert "Evidence replay failed" in capsys.readouterr().err


def test_insufficient_or_constant_temperature_data_is_not_a_replay():
    evidence = bundle()
    evidence["daily"] = evidence["daily"][:2]
    with pytest.raises(ValueError, match="At least three"):
        replay_hlc_evidence(evidence)
    evidence = bundle()
    for row in evidence["daily"]:
        row["delta_t_k"] = 5
    with pytest.raises(ValueError, match="no variation"):
        replay_hlc_evidence(evidence)


def test_machine_status_values_survive_room_name_redaction(thermal_math):
    result = thermal_math._redact_evidence_metrics({
        "hlc": {"status": "valid", "reason": "sensor.private blocked in Valid"},
        "rooms": {"valid": {"status": "collecting"}},
    }, {"gas_meter": "sensor.private", "rooms": {"valid": {"name": "Valid"}}})
    assert result["hlc"]["status"] == "valid"
    assert result["rooms"]["room_1"]["status"] == "collecting"
    assert "sensor.private" not in result["hlc"]["reason"]
    assert "Valid" not in result["hlc"]["reason"]
