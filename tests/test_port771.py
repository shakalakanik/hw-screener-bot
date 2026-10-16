import html_pipeline as hp, screener
def test_side_both():
    assert screener._f_from_template({"side":"both"},"crypto","fbo")["side"] is None
    assert screener._f_from_template({},"crypto","fbo")["side"]==0.0
def test_funcs_exist():
    import hwv1
    for n in ("score_v8","score_long","btc_bias_compute","signal_score_show","turn_day_at"): assert hasattr(hwv1,n)
    for n in ("sig_end","stop_k","ru_fri_cut","algo_reject"): assert hasattr(hp,n)
    assert hp.LONG_HOUR_CAP==4 and hp.RU_STOP_ATR==25 and hp.RU_FRI_H==20


def test_swing_mode():
    import html_pipeline as hp
    f_sw = screener._f_from_template({"_ru_mode": "swing"}, "ru", "brk")
    f_in = screener._f_from_template({}, "ru", "brk")
    assert f_sw["_swing"] and not f_in["_swing"]
    card = {"market": "ru", "strategy": "brk", "side": "SHORT", "last": 100.0, "atr_d": 3.0, "signal_ts": 0,
            "feat": {"sw": 2, "atrD_pct": 0.03}, "fwd": []}
    assert hp.swing_gate(card, f_sw) is True and hp.swing_gate(card, f_in) is False
    assert hp.stop_k(card, 10, f_sw) == 0
    hb = {"market": "ru", "strategy": "brk", "side": "LONG", "last": 100.0, "atr_d": 10.0, "signal_ts": 0,
          "feat": {"atrD_pct": 0.02}, "fwd": []}
    assert hp.swing_gate(hb, f_sw) is False and hp.swing_gate(hb, f_in) is None
    r = hp.recompute(hb, f_sw)
    assert abs(r["risk"] - 2.0) < 1e-9 and r["swm"] == 1          # 50% ATR = 5, кап 2%
    assert abs(hp.recompute(hb, f_in)["risk"] - 2.5) < 1e-9       # 25% ATR


def test_algo_market_removed():
    import storage
    assert storage.ACTIVE_MARKETS == ("crypto", "ru")
    import ai_chat
    assert "algo" not in ai_chat._MKTS
