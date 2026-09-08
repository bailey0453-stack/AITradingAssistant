from app.services.current_calibration import calibration_text


def test_calibration_text_marks_small_sample_provisional():
    text = calibration_text({
        "horizon": "4h",
        "confidence_bucket": "50-70",
        "samples": 8,
        "directional_accuracy": 62.5,
        "target_hit_rate": 37.5,
        "avg_abs_target_error": 0.0412,
        "reliable": False,
        "min_reliable_samples": 20,
    })
    assert "Provisional history" in text
    assert "direction 62.5% (n=8)" in text
    assert "target hit 37.5%" in text
    assert "0.0412 MXN" in text
    assert "need 20 samples" in text


def test_calibration_text_marks_reliable_sample_measured():
    text = calibration_text({
        "horizon": "4h",
        "confidence_bucket": "70-85",
        "samples": 42,
        "directional_accuracy": 64.3,
        "target_hit_rate": 45.2,
        "avg_abs_target_error": 0.0321,
        "reliable": True,
        "min_reliable_samples": 20,
    })
    assert "Measured history" in text
    assert "n=42" in text
    assert "need 20 samples" not in text


def test_calibration_text_handles_no_history():
    text = calibration_text({
        "horizon": "4h",
        "confidence_bucket": "50-70",
        "samples": 0,
    })
    assert "no evaluated 4h forecasts yet" in text
