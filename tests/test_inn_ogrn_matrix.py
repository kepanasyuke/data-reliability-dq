"""Матрица контрольных чисел ИНН/ОГРН.

Эталонная реализация алгоритма ФНС написана здесь независимо от
``reliability.validators``: если поменяется формула в коде, тесты это увидят.
ИНН и ОГРН генерируются детерминированно, поэтому падение воспроизводимо.
"""

from __future__ import annotations

import random

import pytest

from reliability.validators import inn_valid, ogrn_valid

WEIGHTS_10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
WEIGHTS_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
WEIGHTS_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

REAL_INNS = ("7707083893", "7736050003", "500100732259", "7830002293")
REAL_OGRNS = ("1027700132195", "304500116000157")


def _checksum(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(digit) * weight for digit, weight in zip(digits, weights, strict=True))
    return total % 11 % 10


def _random_digits(rng: random.Random, size: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(size))


def _inn10(rng: random.Random) -> str:
    prefix = _random_digits(rng, 9)
    return prefix + str(_checksum(prefix, WEIGHTS_10))


def _inn12(rng: random.Random) -> str:
    prefix = _random_digits(rng, 10)
    d11 = _checksum(prefix, WEIGHTS_11)
    d12 = _checksum(prefix + str(d11), WEIGHTS_12)
    return prefix + str(d11) + str(d12)


def _ogrn(rng: random.Random, size: int) -> str:
    base = str(rng.randint(1, 9)) + _random_digits(rng, size - 2)
    modulus = 11 if size == 13 else 13
    return base + str(int(base) % modulus % 10)


def _corrupt_last(value: str) -> str:
    return value[:-1] + str((int(value[-1]) + 1) % 10)


RNG = random.Random(2026)
VALID_INN10 = [_inn10(RNG) for _ in range(60)]
VALID_INN12 = [_inn12(RNG) for _ in range(40)]
INVALID_INN = [_corrupt_last(value) for value in VALID_INN10[:40] + VALID_INN12[:20]]
VALID_OGRN13 = [_ogrn(RNG, 13) for _ in range(40)]
VALID_OGRN15 = [_ogrn(RNG, 15) for _ in range(20)]
INVALID_OGRN = [_corrupt_last(value) for value in VALID_OGRN13[:25] + VALID_OGRN15[:15]]


@pytest.mark.parametrize("inn", VALID_INN10)
def test_random_inn10_is_valid(inn: str) -> None:
    assert len(inn) == 10
    assert inn_valid(inn) is True


@pytest.mark.parametrize("inn", VALID_INN12)
def test_random_inn12_is_valid(inn: str) -> None:
    assert len(inn) == 12
    assert inn_valid(inn) is True


@pytest.mark.parametrize("inn", REAL_INNS)
def test_real_inn_is_valid(inn: str) -> None:
    assert inn_valid(inn) is True


@pytest.mark.parametrize("inn", INVALID_INN)
def test_broken_control_digit_is_rejected(inn: str) -> None:
    assert inn_valid(inn) is False


@pytest.mark.parametrize("ogrn", VALID_OGRN13)
def test_random_ogrn13_is_valid(ogrn: str) -> None:
    assert len(ogrn) == 13
    assert ogrn_valid(ogrn) is True


@pytest.mark.parametrize("ogrn", VALID_OGRN15)
def test_random_ogrn15_is_valid(ogrn: str) -> None:
    assert len(ogrn) == 15
    assert ogrn_valid(ogrn) is True


@pytest.mark.parametrize("ogrn", REAL_OGRNS)
def test_real_ogrn_is_valid(ogrn: str) -> None:
    assert ogrn_valid(ogrn) is True


@pytest.mark.parametrize("ogrn", INVALID_OGRN)
def test_broken_ogrn_control_digit_is_rejected(ogrn: str) -> None:
    assert ogrn_valid(ogrn) is False


@pytest.mark.parametrize(
    "value",
    ["", " ", None, "770708389", "77070838931", "7707083893000", "77O7083893", "77070838 93"],
)
def test_bad_inn_shapes_are_rejected(value: str | None) -> None:
    assert inn_valid(value) is False


@pytest.mark.parametrize(
    "value",
    ["", " ", None, "102770013219", "10277001321955", "10277001321 5", "Ю2 7700132195"],
)
def test_bad_ogrn_shapes_are_rejected(value: str | None) -> None:
    assert ogrn_valid(value) is False


@pytest.mark.parametrize("inn", VALID_INN10[:10])
def test_inn_with_spaces_around_value_is_accepted(inn: str) -> None:
    assert inn_valid(f"  {inn}  ") is True


@pytest.mark.parametrize("ogrn", VALID_OGRN13[:10])
def test_ogrn_with_spaces_around_value_is_accepted(ogrn: str) -> None:
    assert ogrn_valid(f"\t{ogrn}\n") is True


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4, 5, 6, 7, 8])
def test_changed_prefix_digit_breaks_inn10(position: int) -> None:
    inn = VALID_INN10[0]
    shifted = str((int(inn[position]) + 1) % 10)
    assert inn_valid(inn[:position] + shifted + inn[position + 1:]) is False
