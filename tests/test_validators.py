"""Проверка контрольных чисел ИНН/ОГРН - самый частый источник 'тихих' ошибок
в справочнике клиентов: строка валидна по длине, но не сходится с ФНС."""

from __future__ import annotations

import pytest

from reliability.validators import inn_valid, ogrn_valid


@pytest.mark.parametrize(
    "inn",
    [
        "7707083893",   # Сбербанк
        "7736050003",
        "500100732259",  # 12-значный ИНН (ИП)
    ],
)
def test_valid_inn(inn: str) -> None:
    assert inn_valid(inn) is True


@pytest.mark.parametrize(
    "inn",
    [
        "7707083894",   # испорчено контрольное число
        "770708389",    # 9 знаков
        "77070838931",  # 11 знаков
        "77O7083893",   # буква вместо цифры
        "",
        None,
    ],
)
def test_invalid_inn(inn) -> None:
    assert inn_valid(inn) is False


@pytest.mark.parametrize(
    "ogrn",
    [
        "1027700132195",
        "304500116000157",  # ОГРНИП, 15 знаков
    ],
)
def test_valid_ogrn(ogrn: str) -> None:
    assert ogrn_valid(ogrn) is True


@pytest.mark.parametrize(
    "ogrn",
    ["1027700132196", "304500116000158", "102770013219", "10277001321955", None],
)
def test_invalid_ogrn(ogrn) -> None:
    assert ogrn_valid(ogrn) is False
