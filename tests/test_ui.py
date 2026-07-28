from src.ui import (
    PROFILE_NAMES,
    score_tone,
    signal_tone,
    strategy_profile_defaults,
)


def test_profiles_are_valid_and_return_copies() -> None:
    balanced = strategy_profile_defaults("Equilibrado")
    growth = strategy_profile_defaults("Crecimiento")
    prudent = strategy_profile_defaults("Prudente")

    assert PROFILE_NAMES[-1] == "Personalizado"
    assert balanced["stop_loss"] == 8.0
    assert growth["forward_horizon"] == 40
    assert prudent["max_risk"] == 0.5
    assert prudent["sell_score"] < prudent["reduce_score"] < prudent["watch_score"]

    balanced["stop_loss"] = 99
    assert strategy_profile_defaults("Equilibrado")["stop_loss"] == 8.0


def test_ui_tones_keep_textual_meaning() -> None:
    assert score_tone(80) == "positive"
    assert score_tone(60) == "watch"
    assert score_tone(45) == "caution"
    assert score_tone(20) == "negative"
    assert signal_tone("Vender") == "negative"
    assert signal_tone("Vigilancia") == "watch"
    assert signal_tone("Entrada interesante") == "positive"
