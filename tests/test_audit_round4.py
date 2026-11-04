"""Regression tests for audit round 4: names the docs list under ``discord``."""

from __future__ import annotations

import discord
import discord.enums


def test_documented_enums_are_exported():
    """B2-01: docs/api.rst documents ESRBRating and PromotionType under ``discord``,
    but they were missing from ``enums.__all__``, so ``discord.ESRBRating`` raised."""
    assert discord.ESRBRating is discord.enums.ESRBRating
    assert discord.PromotionType is discord.enums.PromotionType
    assert {'ESRBRating', 'PromotionType'} <= set(discord.enums.__all__)
