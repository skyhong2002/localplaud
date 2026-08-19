"""Traditional-Chinese normalisation safeguards."""

from __future__ import annotations


def test_residential_community_compounds_remain_neighbourhood_terms():
    from localplaud.zh import to_traditional

    assert to_traditional("社区管委会制度与群组管理") == "社區管委會制度與群組管理"
    assert to_traditional("社区规约与住户事务") == "社區規約與住戶事務"


def test_online_community_still_uses_taiwan_social_group_wording():
    from localplaud.zh import to_traditional

    assert to_traditional("线上社区经营") == "線上社群經營"
