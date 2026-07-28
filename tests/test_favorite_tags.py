from src.favorite_tags import (
    favorite_tags_from_value,
    serialize_favorite_tags,
    suggest_favorite_tags,
)


def test_favorite_tags_are_normalized_and_limited() -> None:
    value = favorite_tags_from_value(
        "tecnología, Small cap, tecnología, desconocida, ETF, Energía, Salud, Fondo"
    )

    assert value == ["Tecnología", "Small cap", "ETF", "Energía", "Salud"]
    assert serialize_favorite_tags(value) == (
        "Tecnología, Small cap, ETF, Energía, Salud"
    )


def test_suggestions_combine_sector_and_size() -> None:
    assert suggest_favorite_tags(
        "TSM",
        "Taiwan Semiconductor",
        fundamentals={"sector": "Technology", "marketCap": 500_000_000_000},
    ) == ["Tecnología"]
    assert suggest_favorite_tags(
        "BIOX",
        "Example Therapeutics",
        fundamentals={"sector": "Healthcare", "marketCap": 900_000_000},
    ) == ["Biotecnología", "Small cap"]
    assert suggest_favorite_tags(
        "VTI",
        "Vanguard Total Stock Market ETF",
        instrument_type="ETF",
        fundamentals={"marketCap": 1_000_000},
    ) == ["ETF"]


def test_suggestion_uses_name_when_sector_is_missing() -> None:
    assert suggest_favorite_tags("YPF", "YPF Sociedad Anónima") == ["Energía"]


def test_defense_space_and_quantum_are_recognized() -> None:
    assert suggest_favorite_tags(
        "RKLB",
        "Rocket Lab USA",
        fundamentals={"sector": "Industrials", "industry": "Aerospace & Defense"},
    ) == ["Industria", "Defensa", "Espacio"]
    assert suggest_favorite_tags("IONQ", "IonQ Quantum Computing") == ["Cuántica"]
