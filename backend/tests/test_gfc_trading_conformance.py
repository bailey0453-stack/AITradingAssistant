from __future__ import annotations

import pytest

from app.config import Settings
from app.services.fix.codec import field_map
from app.services.fix.messages import build_sequence_reset_gap_fill
from app.services.fix.trading_messages import build_new_order_single
from app.services.fix.trading_session import CentroidTradingSession


def _settings(**overrides):
    base = dict(
        centroid_td_host="demo.example",
        centroid_td_port=1234,
        centroid_td_username="demo-user",
        centroid_td_password="demo-pass",
        centroid_td_sender_comp_id="CLIENT",
        centroid_td_target_comp_id="GFC",
        centroid_td_account="DEMO",
        centroid_td_enabled=True,
        centroid_td_conformance_mode=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_market_order_builder_contains_required_centroid_fields():
    raw = build_new_order_single(
        seq_num=7,
        sender_comp_id="CLIENT",
        target_comp_id="GFC",
        account="DEMO",
        symbol="USDMXN.r",
        side="1",
        quantity=100000,
        ord_type="1",
        time_in_force="3",
        cl_ord_id="CONF-ORDER-1",
        sending_time="20260909-12:00:00.000",
    )
    msg = field_map(raw)
    assert msg["35"] == "D"
    assert msg["11"] == "CONF-ORDER-1"
    assert msg["1"] == "DEMO"
    assert msg["55"] == "USDMXN.r"
    assert msg["54"] == "1"
    assert msg["38"] == "100000"
    assert msg["40"] == "1"
    assert msg["59"] == "3"
    assert msg["60"] == "20260909-12:00:00.000"


def test_limit_order_requires_price():
    with pytest.raises(ValueError, match="Limit orders require price"):
        build_new_order_single(
            seq_num=1,
            sender_comp_id="CLIENT",
            target_comp_id="GFC",
            account="DEMO",
            symbol="USDMXN.r",
            side="2",
            quantity=100000,
            ord_type="2",
            time_in_force="4",
        )


def test_limit_fok_includes_price_and_tif():
    raw = build_new_order_single(
        seq_num=2,
        sender_comp_id="CLIENT",
        target_comp_id="GFC",
        account="DEMO",
        symbol="USDMXN.r",
        side="2",
        quantity=100000,
        ord_type="2",
        time_in_force="4",
        price=17.25,
        cl_ord_id="CONF-FOK-1",
    )
    msg = field_map(raw)
    assert msg["40"] == "2"
    assert msg["59"] == "4"
    assert msg["44"] == "17.25"


def test_sequence_reset_gap_fill_contains_required_fix_fields():
    raw = build_sequence_reset_gap_fill(
        seq_num=100,
        new_seq_no=125,
        sender_comp_id="CLIENT",
        target_comp_id="GFC",
        sending_time="20260910-16:00:00.000",
    )
    msg = field_map(raw)
    assert msg["35"] == "4"
    assert msg["34"] == "100"
    assert msg["43"] == "Y"
    assert msg["122"] == "20260910-16:00:00.000"
    assert msg["123"] == "Y"
    assert msg["36"] == "125"


def test_sequence_reset_gap_fill_must_advance_sequence():
    with pytest.raises(ValueError, match="greater than seq_num"):
        build_sequence_reset_gap_fill(
            seq_num=100,
            new_seq_no=100,
            sender_comp_id="CLIENT",
            target_comp_id="GFC",
        )


def test_trading_session_is_disabled_by_default():
    settings = Settings()
    assert settings.centroid_td_enabled is False
    assert settings.centroid_td_reset_on_logon is False


def test_conformance_order_cannot_send_when_switch_off():
    session = CentroidTradingSession(_settings(centroid_td_enabled=False))
    with pytest.raises(PermissionError, match="Trading session is disabled"):
        session.send_conformance_order(symbol="USDMXN.r", side="1", quantity=1000)


def test_conformance_order_cannot_send_when_conformance_mode_off():
    session = CentroidTradingSession(_settings(centroid_td_conformance_mode=False))
    with pytest.raises(PermissionError, match="Conformance mode is disabled"):
        session.send_conformance_order(symbol="USDMXN.r", side="1", quantity=1000)
