"""HTML 7.90/7.91 cryptoThuFriBlock + 7.75 noOff + 7.85 stopmin + 7.86 warm-up — строгая привязка к рынку/дню/часу."""
import calendar, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import html_pipeline as hp

H = 3_600_000

def msk(y, mo, d, h, mi=0):
    return calendar.timegm((y, mo, d, h - 3, mi, 0)) * 1000

def card(entry_msk, side="SHORT", market="crypto", strategy="fbo", wall=None, src30=False):
    se = entry_msk - (H // 2 if src30 else H)
    return {"signal_ts": se, "src30": src30, "side": side, "market": market,
            "strategy": strategy, "feat": {"wall_r": wall} if wall is not None else {}}

# 2026-10-15 = четверг, 16 = пятница, 17 = суббота, 18 = воскресенье
def test_thu_fri_boundaries():
    B = hp.crypto_thu_fri_block
    assert not B(card(msk(2026, 10, 15, 15, 59)))
    assert B(card(msk(2026, 10, 15, 16, 0)))
    assert B(card(msk(2026, 10, 15, 23, 59)))
    assert B(card(msk(2026, 10, 16, 0, 0)))
    assert B(card(msk(2026, 10, 16, 15, 59)))
    assert not B(card(msk(2026, 10, 16, 16, 0)))

def test_sat_window_wall():
    B = hp.crypto_thu_fri_block
    assert not B(card(msk(2026, 10, 17, 5, 59)))
    assert B(card(msk(2026, 10, 17, 6, 0)))
    assert B(card(msk(2026, 10, 17, 11, 59), wall=6.9))
    assert not B(card(msk(2026, 10, 17, 11, 59), wall=7))
    assert not B(card(msk(2026, 10, 17, 12, 0)))

def test_scope_only_crypto_fbo_short():
    B = hp.crypto_thu_fri_block
    t = msk(2026, 10, 15, 18)
    assert not B(card(t, side="LONG"))
    assert not B(card(t, strategy="brk"))
    assert not B(card(t, market="ru"))
    assert not B(card(msk(2026, 10, 14, 18)))       # среда
    assert not B(card(msk(2026, 10, 18, 9)))        # воскресенье
    assert not B(card(msk(2026, 10, 12, 9)))        # понедельник

def test_src30_uses_sig_end():
    # бар 30-минутки 15:00 МСК → вход 15:30 (вне окна); часовой бар 15:00 → вход 16:00 (в окне)
    bar = msk(2026, 10, 15, 15, 0)
    assert not hp.crypto_thu_fri_block({"signal_ts": bar, "src30": True, "side": "SHORT", "feat": {}})
    assert hp.crypto_thu_fri_block({"signal_ts": bar, "src30": False, "side": "SHORT", "feat": {}})

def test_no_off_moex_only():
    sat, sun, fri = (card(msk(2026, 10, d, 12), market="ru") for d in (17, 18, 16))
    assert hp.no_off([sat, sun, fri], "ru") == [fri]
    assert len(hp.no_off([sat, sun, fri], "crypto")) == 3
    # пятница 23:00 бар → вход сб 00:00 → выходной
    assert hp.ru_off_day({"signal_ts": msk(2026, 10, 16, 23), "side": "LONG"})

def test_stopmin_default():
    assert hp.stop_min_c({}) == 0.02
    assert hp.stop_min_c(None) == 0.02
    assert abs(hp.stop_min_c({"stopmin": 3.5}) - 0.035) < 1e-12
    assert hp.stop_min_c({"stopmin": 0}) == 0.02

def test_warm_cut():
    assert hp.SCAN_WARM_H == 168
    now = msk(2026, 10, 16, 12)
    assert hp.warm_cut(12, now) == now - 180 * H
