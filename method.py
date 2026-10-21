#!/usr/bin/env python3
"""Ac 的 ETH 技术交易方法（部署在 Muse 侧，可直接调用版 v1.0，2026-10-21）

用法: python3 method.py（脚本同目录可放 method_config.json 与 position.json，见 README）
输出: JSON（stdout）+ 保存到脚本同目录 last_output.json

方法 = Ac 现行策略的技术层：
  周线定战略背景 / 日线定趋势 / 4H 触发 / 1H 确认 / 15m-30m 只优化挂单价（本工具用到1H）
  指标: EMA20/50/200、MACD(12,26,9)、RSI(14) Wilder、KDJ(9,3,3)、ADX(14) Wilder、量比
  仓位: 核心50 + 卫星（position.json 读入），信号按策略给份数与市价/限价
铁律: ADX 必须 Wilder（函数从 calc_tier1.py 原样复制，已通过锚点回归）；
  每次运行先做锚点回归校验（2026-10-01T16:00Z 4H K线 ADX 应 ≈19.37），不过则不给信号。
只用已收盘 K 线判断；实时价只用于限价备选的偏离判断。
"""
import json, math, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE_DIR, "last_output.json")
ANCHOR_TS = 1790870400  # 2026-10-01T16:00:00Z
ANCHOR_ADX = 19.37
ANCHOR_TOL = 0.30

# 箱体由工具按 K 线自动判定（2026-10-21 Ac 指示：“箱体你根据K线行情自己判断”）：
# 以最新已收盘 K 线之前的 60 根 4H 高低范围为动态箱体，下沿买入区 = 箱体底部 35%，
# 上沿区 = 箱体顶部 8%，深跌区 = 箱体下沿下方 2.4%~4.2%，突破位 = 箱体上沿。
# 止盈阶梯属于实盘参数、不写进代码（与 Notion 提示词同一原则：只存方法，不存正在交易的东西）：
# 从脚本同目录 method_config.json 读取（格式 {"ladder": [[价格, 份数], ...]}）；
# 文件不存在时用下面的示例阶梯，并在输出 ladder_source 里标注 "example"。
EXAMPLE_LADDER = [(3000, 10), (4000, 15), (5000, 10), (6000, 15)]

def _num(x):
    f = float(x)
    return int(f) if f.is_integer() else f

def load_ladder():
    try:
        cfg = json.load(open(os.path.join(BASE_DIR, "method_config.json")))
        lad = [(_num(p), _num(s)) for p, s in cfg["ladder"]]
        if lad:
            return lad, "config"
    except Exception:
        pass
    return list(EXAMPLE_LADDER), "example"

# ---------------- 数据 ----------------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "trade-method/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_ohlc(pair, interval, key):
    d = _get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
    rows = d["result"][key]
    now = time.time()
    out = []
    for r in rows:
        t = int(r[0])
        if t + interval * 60 > now:  # 未收盘的正在形成 K 线，剔除
            continue
        out.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[6])})
    return out

def fetch_ticker():
    d = _get("https://api.kraken.com/0/public/Ticker?pair=ETHUSD")
    return float(d["result"]["XETHZUSD"]["c"][0])

# ---------------- 指标（ema/wilder_sm/adx_wilder 从 calc_tier1.py 原样复制） ----------------

def ema(vals, period):
    k = 2.0 / (period + 1)
    out = [None] * len(vals)
    seed = sum(vals[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(vals)):
        out[i] = vals[i] * k + out[i - 1] * (1 - k)
    return out

def wilder_sm(vals):
    n = len(vals)
    out = [None] * n
    buf = []
    for i in range(n):
        v = vals[i]
        if v is None:
            continue
        buf.append((i, v))
        if len(buf) < 14:
            continue
        if len(buf) == 14:
            out[i] = sum(x[1] for x in buf)
        else:
            out[i] = out[buf[-2][0]] * 13.0 / 14.0 + v / 14.0
    return out

def adx_wilder(highs, lows, closes, period=14):
    n = len(closes)
    tr = [None] * n
    pdm = [None] * n
    mdm = [None] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    smtr = wilder_sm(tr)
    smp = wilder_sm(pdm)
    smm = wilder_sm(mdm)
    pdi = [None] * n
    mdi = [None] * n
    dx = [None] * n
    for i in range(n):
        if smtr[i] is not None and smtr[i] > 0:
            pdi[i] = 100.0 * smp[i] / smtr[i]
            mdi[i] = 100.0 * smm[i] / smtr[i]
            dsum = pdi[i] + mdi[i]
            dx[i] = 100.0 * abs(pdi[i] - mdi[i]) / dsum if dsum > 0 else 0.0
    adx = [None] * n
    buf = []
    for i in range(n):
        if dx[i] is None:
            continue
        buf.append((i, dx[i]))
        if len(buf) < period:
            continue
        if len(buf) == period:
            adx[i] = sum(x[1] for x in buf) / period
        else:
            adx[i] = adx[buf[-2][0]] * (period - 1.0) / period + dx[i] / period
    return adx, pdi, mdi

def macd_series(closes):
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(ema12, ema26)]
    dif_valid = [x for x in dif if x is not None]
    dea_raw = ema(dif_valid, 9)
    dea = [None] * len(dif)
    base = len(dif) - len(dif_valid)
    for j, v in enumerate(dea_raw):
        dea[base + j] = v
    hist = [None if (d is None or e is None) else d - e for d, e in zip(dif, dea)]
    return dif, dea, hist

def rsi_wilder(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    ag = sum(gains[1:period + 1]) / period
    al = sum(losses[1:period + 1]) / period
    def val(a, l):
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + a / l)
    out[period] = val(ag, al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i] = val(ag, al)
    return out

def kdj(highs, lows, closes, n=9):
    N = len(closes)
    K = [None] * N; D = [None] * N; J = [None] * N
    k_prev, d_prev = 50.0, 50.0
    for i in range(N):
        if i < n - 1:
            continue
        hh = max(highs[i - n + 1:i + 1]); ll = min(lows[i - n + 1:i + 1])
        rsv = 0.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        k = 2.0 / 3 * k_prev + 1.0 / 3 * rsv
        d = 2.0 / 3 * d_prev + 1.0 / 3 * k
        K[i] = k; D[i] = d; J[i] = 3 * k - 2 * d
        k_prev, d_prev = k, d
    return K, D, J

def last2(arr):
    """返回 (最新值, 上一值)，跳过 None"""
    vals = [x for x in arr if x is not None]
    if len(vals) >= 2:
        return vals[-1], vals[-2]
    if len(vals) == 1:
        return vals[-1], None
    return None, None

# ---------------- 周期分析 ----------------

def fmt_ts(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(CST).strftime("%m-%d %H:%M")

def analyze(candles, with_kdj=False, with_vol=False):
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    e20 = ema(closes, 20); e50 = ema(closes, 50); e200 = ema(closes, 200)
    dif, dea, hist = macd_series(closes)
    adx, pdi, mdi = adx_wilder(highs, lows, closes)
    rsi = rsi_wilder(closes)
    last = candles[-1]; prev = candles[-2]
    out = {
        "n": len(candles),
        "candle": f"{fmt_ts(last['t'])} CST 开盘",
        "close": round(last["c"], 2), "prev_close": round(prev["c"], 2),
        "pct_vs_prev": round(100 * (last["c"] / prev["c"] - 1), 2),
        "high": round(last["h"], 2), "low": round(last["l"], 2),
        "ema20": round(e20[-1], 2) if e20[-1] else None,
        "ema20_prev": round(e20[-2], 2) if e20[-2] else None,
        "ema50": round(e50[-1], 2) if e50[-1] else None,
        "ema200": round(e200[-1], 2) if e200[-1] else None,
        "above_ema20": last["c"] > e20[-1] if e20[-1] else None,
        "dif": round(dif[-1], 2) if dif[-1] is not None else None,
        "dea": round(dea[-1], 2) if dea[-1] is not None else None,
        "hist": round(hist[-1], 3) if hist[-1] is not None else None,
        "hist_prev": round(hist[-2], 3) if hist[-2] is not None else None,
        "adx": round(adx[-1], 2) if adx[-1] is not None else None,
        "adx_prev": round(adx[-2], 2) if adx[-2] is not None else None,
        "pdi": round(pdi[-1], 2) if pdi[-1] is not None else None,
        "mdi": round(mdi[-1], 2) if mdi[-1] is not None else None,
        "rsi": round(rsi[-1], 1) if rsi[-1] is not None else None,
        "rsi_prev": round(rsi[-2], 1) if rsi[-2] is not None else None,
    }
    # 最近一次 MACD 交叉（在已收盘 K 线序列中定位）
    cross = None
    for i in range(len(closes) - 1, max(len(closes) - 20, 1), -1):
        if dif[i] is None or dea[i] is None or dif[i - 1] is None or dea[i - 1] is None:
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            cross = {"type": "golden", "candles_ago": len(closes) - 1 - i,
                     "signal_close": closes[i], "signal_hist": hist[i]}
            break
        if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            cross = {"type": "death", "candles_ago": len(closes) - 1 - i,
                     "signal_close": closes[i], "signal_hist": hist[i]}
            break
    out["last_cross"] = cross
    if with_kdj:
        K, D, J = kdj(highs, lows, closes)
        out["kdj"] = {"k": round(K[-1], 1), "d": round(D[-1], 1), "j": round(J[-1], 1),
                      "j_prev": round(J[-2], 1)}
    if with_vol:
        vols = [c["v"] for c in candles]
        ma20 = sum(vols[-21:-1]) / 20 if len(vols) > 20 else None
        out["volume"] = round(last["v"], 1)
        out["vol_ma20_prev"] = round(ma20, 1) if ma20 else None
        out["vol_ratio"] = round(last["v"] / ma20, 2) if ma20 else None
    # K 线形态（最新已收盘）
    rng = last["h"] - last["l"]
    if rng > 0:
        out["lower_shadow_pct"] = round(100 * (min(last["o"], last["c"]) - last["l"]) / rng, 0)
        out["upper_shadow_pct"] = round(100 * (last["h"] - max(last["o"], last["c"])) / rng, 0)
    return out, {"closes": closes, "highs": highs, "lows": lows,
                 "dif": dif, "dea": dea, "hist": hist, "adx": adx}

# ---------------- 背景层（只读，不参与信号） ----------------

def bg_btc():
    try:
        c = fetch_ohlc("XBTUSD", 240, "XXBTZUSD")
        closes = [x["c"] for x in c]
        e20 = ema(closes, 20)
        dif, dea, hist = macd_series(closes)
        return {"close": round(closes[-1], 0), "ema20": round(e20[-1], 0),
                "above_ema20": closes[-1] > e20[-1],
                "macd": "金叉侧" if dif[-1] > dea[-1] else "死叉侧",
                "hist_rising": hist[-1] > hist[-2]}
    except Exception as e:
        return {"error": str(e)[:80]}

def bg_sentiment():
    out = {}
    try:
        d = _get("https://api.alternative.me/fng/?limit=1", timeout=15)
        out["fear_greed"] = int(d["data"][0]["value"])
        out["fear_greed_label"] = d["data"][0]["value_classification"]
    except Exception as e:
        out["fear_greed_error"] = str(e)[:60]
    try:
        d = _get("https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP", timeout=15)
        out["funding_pct"] = round(float(d["data"][0]["fundingRate"]) * 100, 4)
    except Exception as e:
        out["funding_error"] = str(e)[:60]
    try:
        d = _get("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=ETH-USDT-SWAP", timeout=15)
        row = d["data"][0]
        out["oi_eth"] = round(float(row["oiCcy"]), 0)   # 持仓量（ETH 本位）
        out["oi_usd"] = round(float(row["oiUsd"]), 0)   # 名义价值（USD）
    except Exception as e:
        out["oi_error"] = str(e)[:60]
    return out

# ---------------- 判断引擎 ----------------

def judge(W, D, H4, H1, raw4, live, pos, ladder):
    spot = pos.get("eth_pct", 0); cash = pos.get("cash_pct", 0)
    satellite = max(0, spot - 50)
    sigs = []   # 可执行信号
    notes = []  # 观察项
    h4c = H4["close"]

    weekly_broken = W["above_ema20"] is False
    daily_broken = D["above_ema20"] is False

    # 动态箱体：取最新已收盘 K 线之前的 60 根 4H 高低范围
    # （不含正在判断的最新一根，否则收盘价永远突破不了上沿）
    closes4 = raw4["closes"]; highs4 = raw4["highs"]; lows4 = raw4["lows"]
    seg_hi = highs4[-61:-1] if len(highs4) > 60 else highs4[:-1]
    seg_lo = lows4[-61:-1] if len(lows4) > 60 else lows4[:-1]
    box_hi = max(seg_hi); box_lo = min(seg_lo)
    width = box_hi - box_lo
    box60_hi, box60_lo = box_hi, box_lo
    box30_hi = max(highs4[-31:-1]); box30_lo = min(lows4[-31:-1])
    low_zone = (box_lo, box_lo + 0.35 * width)          # 下沿买入区：箱体底部 35%
    up_zone = (box_hi - 0.08 * width, box_hi)           # 上沿区：箱体顶部 8%
    deep_zone = (box_lo * 0.958, box_lo * 0.976)        # 深跌区：下沿下方 4.2%~2.4%
    breakout_lvl = box_hi                               # 突破位 = 箱体上沿

    # 4H regime
    adx = H4["adx"] or 0
    range_regime = adx < 20
    trend_up = (not range_regime) and (H4["pdi"] or 0) > (H4["mdi"] or 0)
    trend_dn = (not range_regime) and (H4["mdi"] or 0) > (H4["pdi"] or 0)
    regime = "区间震荡" if range_regime else ("趋势向上" if trend_up else "趋势向下" if trend_dn else "趋势形成中")

    stabilized = (
        (H4.get("lower_shadow_pct") or 0) >= 40
        or (H4["kdj"]["j"] > H4["kdj"]["j_prev"] and H4["kdj"]["j"] < 40)
        or ((H4["rsi"] or 50) > (H4["rsi_prev"] or 50) and (H4["rsi"] or 50) < 45)
    )
    stalling = (
        (H4.get("upper_shadow_pct") or 0) >= 40
        or (H4["kdj"]["j"] < H4["kdj"]["j_prev"] and H4["kdj"]["j"] > 60)
    )
    below_deep = h4c < deep_zone[0]

    # —— 风控：只在“首次跌破”的新鲜交叉上触发；持续在下方是状态，不每日重复触发 ——
    def fresh_break(T):
        if T.get("ema20_prev") is None:
            return False
        return T["prev_close"] >= T["ema20_prev"] and T["close"] < T["ema20"]
    if weekly_broken:
        if fresh_break(W):
            sigs.append({"prio": 0, "type": "周线风控", "action": "卖出", "size": 20,
                         "exec": "市价", "reason": f"周收盘 {W['close']} 首次跌破周EMA20 {W['ema20']}，战略背景破坏，核心可动，先减 20/100"})
        else:
            notes.append(f"周线收盘持续在周 EMA20（{W['ema20']}）下方：风控状态持续中，该规则只在首次跌破时触发，不重复")
    if daily_broken:
        if fresh_break(D):
            sigs.append({"prio": 1, "type": "日线风控", "action": "卖出", "size": 20,
                         "exec": "市价", "reason": f"日收盘 {D['close']} 首次跌破日EMA20 {D['ema20']}，趋势转弱，减仓 20/100"})
        else:
            notes.append(f"日线收盘 {D['close']} 持续在日 EMA20（{D['ema20']}）下方：风控状态持续中（只在首次跌破触发一次，不重复）；重新站回前，4H 反弹不等于趋势反转")
    # —— 4H MACD 交叉确认 ——
    cross = H4.get("last_cross")
    if cross and cross["candles_ago"] > 2:
        notes.append(f"最近一次 4H MACD {'金叉' if cross['type'] == 'golden' else '死叉'} 发生在 {cross['candles_ago']} 根 K 线前，已非新鲜信号，不重复触发")
    if cross and cross["candles_ago"] <= 2:
        ago = cross["candles_ago"]
        confirm2 = ago >= 1 and ((H4["hist"] or 0) * (1 if cross["type"] == "golden" else -1) > 0)
        strong1 = abs(cross.get("signal_hist") or 0) > 1.0
        confirmed = confirm2 or (ago == 0 and strong1)
        if cross["type"] == "golden" and H4["above_ema20"]:
            if confirmed and not range_regime and cash >= 10:
                s = {"prio": 3, "type": "4H MACD 金叉（确认）", "action": "买入", "size": 10,
                     "exec": "市价", "reason": f"金叉{ago}根前发生且经确认，收盘站上 4H EMA20（{H4['ema20']}），非区间市"}
                dev = 100 * (cross["signal_close"] / live - 1) if live else 0
                if dev > 1:  # 现价已较信号价回撤 >1% → 限价备选
                    s["limit_alt"] = f"现价 {live} 已较信号价回撤 {dev:.1f}%，可改限价挂 4H EMA20 {H4['ema20']} 附近"
                sigs.append(s)
            else:
                notes.append(f"4H 出现金叉但未满足确认/非区间条件（确认={confirmed}，区间市={range_regime}），不单独行动")
        if cross["type"] == "death" and not H4["above_ema20"]:
            if confirmed and not range_regime and satellite > 0:
                s = {"prio": 3, "type": "4H MACD 死叉（确认）", "action": "卖出", "size": min(10, satellite),
                     "exec": "市价", "reason": f"死叉{ago}根前发生且经确认，收盘跌破 4H EMA20（{H4['ema20']}），非区间市，清卫星"}
                dev = 100 * (live / cross["signal_close"] - 1) if live else 0
                if dev > 1:  # 现价已较信号价反弹 >1% → 限价备选（挂实际受压线）
                    s["limit_alt"] = f"现价 {live} 已较信号价反弹 {dev:.1f}%，可改限价挂受压线 4H EMA20 {H4['ema20']} 附近卖出"
                sigs.append(s)
            else:
                notes.append(f"4H 出现死叉但未满足确认/非区间/有卫星条件（确认={confirmed}，区间市={range_regime}，卫星={satellite}），不单独行动")
    # —— 区间下沿 / 深跌区 ——
    if low_zone[0] <= h4c <= low_zone[1] and cash >= 10 and not below_deep:
        if stabilized:
            sigs.append({"prio": 4, "type": "区间下沿企稳", "action": "买入", "size": 10,
                         "exec": "限价", "limit_ref": round(min(lows4[-6:]), 1),
                         "reason": f"收盘 {h4c} 进入动态箱体下沿区 {low_zone[0]:.0f}-{low_zone[1]:.0f} 且出现企稳（长下影/KDJ拐头/RSI回升）"})
        else:
            notes.append(f"价格在动态箱体下沿区 {low_zone[0]:.0f}-{low_zone[1]:.0f} 内，但尚无企稳确认（下影 {H4.get('lower_shadow_pct')}%，KDJ J {H4['kdj']['j']}），等确认再买")
    if deep_zone[0] <= h4c <= deep_zone[1] and cash >= 10:
        if stabilized:
            sigs.append({"prio": 4, "type": "深跌区企稳", "action": "买入", "size": 10,
                         "exec": "限价", "limit_ref": round(min(lows4[-6:]), 1),
                         "reason": f"收盘 {h4c} 进入动态深跌区 {deep_zone[0]:.0f}-{deep_zone[1]:.0f} 且企稳"})
        else:
            notes.append(f"价格在动态深跌区 {deep_zone[0]:.0f}-{deep_zone[1]:.0f}，尚无企稳确认，不接飞刀")
    if below_deep:
        notes.append(f"4H 收盘 {h4c} 已跌至动态箱体深跌区（{deep_zone[0]:.0f}-{deep_zone[1]:.0f}）之下：买入只认‘企稳 + 交叉确认’的组合，单一指标不行动")
    # —— 区间上沿 ——
    if up_zone[0] <= h4c <= up_zone[1] and satellite > 0:
        if stalling:
            sigs.append({"prio": 5, "type": "区间上沿滞涨", "action": "卖出", "size": min(10, satellite),
                         "exec": "限价", "limit_ref": round(max(highs4[-6:]), 1),
                         "reason": f"收盘 {h4c} 到动态箱体上沿区 {up_zone[0]:.0f}-{up_zone[1]:.0f} 且滞涨（长上影/KDJ高位拐头）"})
        else:
            notes.append(f"价格在区间上沿附近，尚无滞涨确认，暂不卖卫星")
    # —— 突破 ——
    adx_cross_up = (H4["adx_prev"] or 0) < 20 <= adx
    if h4c > breakout_lvl and (H4.get("vol_ratio") or 0) > 1.0 and (adx_cross_up or (adx > 20 and (H4["adx"] or 0) > (H4["adx_prev"] or 0))) and cash >= 10:
        sigs.append({"prio": 4, "type": "放量突破", "action": "买入", "size": 10, "exec": "市价",
                     "reason": f"4H 收盘 {h4c} 站上动态箱体上沿 {breakout_lvl:.0f}，量比 {H4.get('vol_ratio')}，ADX {adx} 上行；失效：收盘跌回 {breakout_lvl:.0f} 下方"})
    # —— 强势回踩 ——
    if D["above_ema20"] and H4["above_ema20"] and cash >= 10:
        near_4h = abs(h4c / H4["ema20"] - 1) < 0.007 if H4["ema20"] else False
        near_d = abs(h4c / D["ema20"] - 1) < 0.007 if D["ema20"] else False
        if (near_4h or near_d) and stabilized:
            line = H4["ema20"] if near_4h else D["ema20"]
            sigs.append({"prio": 5, "type": "强势回踩 EMA20", "action": "买入", "size": 10,
                         "exec": "限价", "limit_ref": line,
                         "reason": f"日线站上日EMA20 的强势格局下，价格回踩 {'4H' if near_4h else '日线'} EMA20（{line}）附近并企稳"})
    # —— 止盈梯子 ——
    nxt = next(((p, s) for p, s in ladder if (live or h4c) < p), None)
    if live and live >= ladder[0][0]:
        sigs.append({"prio": 2, "type": "到达止盈梯子", "action": "卖出", "size": ladder[0][1], "exec": "市价",
                     "reason": f"现价 {live} 已达第一档止盈 {ladder[0][0]}，按梯子卖 {ladder[0][1]}/100（更高档：{ladder[1:]}）"})
    # —— 1H 背离警讯（只提示） ——
    d1 = raw4.get("h1_div")
    if d1:
        notes.append(d1)

    sigs.sort(key=lambda s: s["prio"])
    verdict = sigs[0] if sigs else None
    return {"regime_4h": regime, "range_regime": range_regime,
            "dynamic_box": {"low": round(box_lo, 1), "high": round(box_hi, 1),
                            "lower_zone": [round(low_zone[0], 1), round(low_zone[1], 1)],
                            "upper_zone": [round(up_zone[0], 1), round(up_zone[1], 1)],
                            "deep_zone": [round(deep_zone[0], 1), round(deep_zone[1], 1)],
                            "basis": "最新已收盘K线之前60根4H高低范围"},
            "box60": [round(box60_lo, 1), round(box60_hi, 1)],
            "box30": [round(box30_lo, 1), round(box30_hi, 1)],
            "satellite": satellite,
            "next_ladder": ({"price": nxt[0], "size": nxt[1],
                             "dist_pct": round(100 * (nxt[0] / (live or h4c) - 1), 2)} if nxt else None),
            "verdict": verdict, "other_signals": sigs[1:], "notes": notes}

# ---------------- 主流程 ----------------

def main():
    started = datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")
    c4 = fetch_ohlc("ETHUSD", 240, "XETHZUSD")
    cd = fetch_ohlc("ETHUSD", 1440, "XETHZUSD")
    cw = fetch_ohlc("ETHUSD", 10080, "XETHZUSD")
    c1 = fetch_ohlc("ETHUSD", 60, "XETHZUSD")
    try:
        live = fetch_ticker()
    except Exception:
        live = c4[-1]["c"]

    # 锚点回归（铁律）：在 4H 序列中找 2026-10-01T16:00Z K 线
    highs4 = [c["h"] for c in c4]; lows4 = [c["l"] for c in c4]; closes4 = [c["c"] for c in c4]
    adx_arr, _, _ = adx_wilder(highs4, lows4, closes4)
    anchor = {"expected": ANCHOR_ADX, "tol": ANCHOR_TOL}
    idx = next((i for i, c in enumerate(c4) if c["t"] == ANCHOR_TS), None)
    if idx is None:
        anchor["status"] = "skipped_anchor_out_of_window"
    else:
        val = adx_arr[idx]
        anchor["value"] = round(val, 2) if val is not None else None
        anchor["status"] = "pass" if (val is not None and abs(val - ANCHOR_ADX) <= ANCHOR_TOL) else "FAIL"

    W, _ = analyze(cw)
    D, _ = analyze(cd)
    H4, raw4 = analyze(c4, with_kdj=True, with_vol=True)
    H1, raw1 = analyze(c1)
    # 1H 背离简查：近20根新高/新低 vs DIF
    div = None
    c1c = raw1["closes"]; dif1 = raw1["dif"]
    if len(c1c) >= 21 and dif1[-1] is not None:
        if c1c[-1] >= max(c1c[-21:-1]) and dif1[-1] < max(x for x in dif1[-21:-1] if x is not None):
            div = "1H 警讯：价格新高但 DIF 未新高（顶背离雏形），只作警讯不单独触发"
        if c1c[-1] <= min(c1c[-21:-1]) and dif1[-1] > min(x for x in dif1[-21:-1] if x is not None):
            div = "1H 警讯：价格新低但 DIF 未新低（底背离雏形），只作警讯不单独触发"
    raw4["h1_div"] = div

    pos = {"eth_pct": 0, "cash_pct": 100}
    try:
        pos = json.load(open(os.path.join(BASE_DIR, "position.json")))
    except Exception:
        pass
    ladder, ladder_src = load_ladder()

    out = {"run_at": started, "live_price": round(live, 2),
           "anchor_regression": anchor,
           "ladder_source": ladder_src,
           "position": {"spot": pos.get("eth_pct"), "cash": pos.get("cash_pct")},
           "weekly": W, "daily": D, "h4": H4, "h1": H1,
           "background": {"btc": bg_btc(), "sentiment": bg_sentiment(),
                          "note": "背景层只记录、不触发（影子模式）"}}
    if anchor["status"] == "FAIL":
        out["verdict_blocked"] = "ADX 锚点回归失败，按铁律不输出交易信号；请先排查指标实现"
    else:
        out["judgment"] = judge(W, D, H4, H1, raw4, live, pos, ladder)
    ethbtc = None
    try:
        b = out["background"]["btc"]
        if b.get("close"):
            ethbtc = round(live / b["close"], 5)
    except Exception:
        pass
    out["background"]["eth_btc"] = ethbtc
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
