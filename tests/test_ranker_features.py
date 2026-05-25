from trading.strategy.ranker_features import FEATURE_NAMES


def test_feature_names_is_a_tuple_of_20_unique_strings() -> None:
    assert isinstance(FEATURE_NAMES, tuple)
    assert len(FEATURE_NAMES) == 20
    assert len(set(FEATURE_NAMES)) == 20  # unique
    assert all(isinstance(n, str) and n for n in FEATURE_NAMES)
