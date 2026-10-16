import html_pipeline as hp, screener
def test_side_both():
    assert screener._f_from_template({"side":"both"},"crypto","fbo")["side"] is None
    assert screener._f_from_template({},"crypto","fbo")["side"]==0.0
def test_funcs_exist():
    import hwv1
    for n in ("score_v8","score_long","btc_bias_compute","signal_score_show","turn_day_at"): assert hasattr(hwv1,n)
    for n in ("sig_end","stop_k","ru_fri_cut","algo_reject"): assert hasattr(hp,n)
    assert hp.LONG_HOUR_CAP==4 and hp.RU_STOP_ATR==25 and hp.RU_FRI_H==20
