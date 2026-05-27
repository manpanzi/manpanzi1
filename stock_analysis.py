#!/usr/bin/env python3
"""
A-share Stock Analysis & WeChat Work Alert System
分析A股持仓并推送至企业微信，含均线、量价、趋势综合研判。

数据源: 新浪(实时行情) + 腾讯(日K线)
运行: python stock_analysis.py (需设置环境变量 WECHAT_WEBHOOK_URL)
"""

import requests
import json
import sys
import os
import time as _time
import random
from datetime import datetime, time
from collections import OrderedDict

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ===================== CONFIG =====================

STOCKS = OrderedDict({
    "sz159513": {"name": "纳斯达克大成", "type": "ETF"},
    "sz159326": {"name": "电网华夏",     "type": "ETF"},
    "sz159995": {"name": "芯片华夏",     "type": "ETF"},
    "sz300859": {"name": "西域旅游",     "type": "股票"},
})

WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "")
if not WEBHOOK_URL:
    print("[ERROR] 环境变量 WECHAT_WEBHOOK_URL 未设置")
    sys.exit(1)

# ===================== NETWORK =====================

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def http_get(url, params=None, referer=None, timeout=15, encoding=None):
    """Generic HTTP GET with retry."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            return resp
        except Exception:
            if attempt < 2:
                _time.sleep((attempt + 1) * 2.0 + random.uniform(0, 1))
    raise Exception(f"HTTP request failed after 3 retries: {url}")

# ===================== DATA =====================

def fetch_kline_tencent(code, days=60):
    """拉取日K线 (前复权) from Tencent API.
    K-line format: [date, open, close, high, low, volume]
    """
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{code},day,,,{days},qfq"}
    try:
        resp = http_get(url, params=params, referer="https://gu.qq.com/")
        data = resp.json()
        stock_data = data.get("data", {}).get(code, {})
        raw_klines = stock_data.get("qfqday") or stock_data.get("day") or []
        result = []
        for k in raw_klines:
            result.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]),
                "amount": 0,
                "amplitude": 0,
                "change_pct": 0,
                "change": 0,
                "turnover": 0,
            })
        for i in range(1, len(result)):
            prev = result[i - 1]["close"]
            if prev > 0:
                result[i]["change_pct"] = (result[i]["close"] / prev - 1) * 100
                result[i]["change"] = result[i]["close"] - prev
        return result
    except Exception as e:
        print(f"    [WARN] Tencent K-line failed: {e}")
        return []

def fetch_realtime_sina(codes):
    """拉取实时行情 from Sina API (batch, all stocks in one request).

    Sina format fields:
      0: name, 1: open, 2: prev_close, 3: price, 4: high, 5: low,
      6: bid1, 7: ask1, 8: volume, 9: amount, ...
      30: date, 31: time
    """
    code_str = ",".join(codes)
    url = "http://hq.sinajs.cn/list=" + code_str
    try:
        resp = http_get(url, referer="https://finance.sina.com.cn/", encoding="gbk")
        text = resp.text
    except Exception as e:
        print(f"    [WARN] Sina realtime failed: {e}")
        return {}

    results = {}
    for line in text.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        var_name, var_value = line.split("=", 1)
        code = var_name.replace("var hq_str_", "").strip()
        content = var_value.strip().strip('"').strip(";")
        parts = content.split(",")
        if len(parts) < 32:
            continue

        prev_close = float(parts[2]) if parts[2] else 0
        price = float(parts[3]) if parts[3] else 0
        t_open = float(parts[1]) if parts[1] else 0
        t_high = float(parts[4]) if parts[4] else 0
        t_low = float(parts[5]) if parts[5] else 0
        volume = float(parts[8]) if parts[8] else 0
        amount = float(parts[9]) if parts[9] else 0

        chg_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0

        results[code] = {
            "price": price,
            "open": t_open,
            "high": t_high,
            "low": t_low,
            "volume": volume,
            "amount": amount,
            "vol_ratio": 1.0,
            "change_pct": chg_pct,
            "change": price - prev_close,
        }
    return results

# ===================== INDICATORS =====================

def ma(data, n):
    if len(data) < n:
        return None
    return sum(data[-n:]) / n

def ema(data, n):
    """Exponential Moving Average."""
    if len(data) < n:
        return None
    k = 2.0 / (n + 1)
    val = sum(data[:n]) / n
    for x in data[n:]:
        val = (x - val) * k + val
    return val

def ema_list(data, n):
    """Return full EMA series (same length as input)."""
    if len(data) < n:
        return [None] * len(data)
    k = 2.0 / (n + 1)
    result = [None] * (n - 1)
    val = sum(data[:n]) / n
    result.append(val)
    for x in data[n:]:
        val = (x - val) * k + val
        result.append(val)
    return result

def calc_macd(closes):
    """Return (DIF, DEA, MACD_hist) for the latest day, plus status string."""
    ema12_list = ema_list(closes, 12)
    ema26_list = ema_list(closes, 26)
    if not ema12_list or not ema26_list:
        return None, None, None, "—"

    dif_series = []
    for e12, e26 in zip(ema12_list, ema26_list):
        if e12 is not None and e26 is not None:
            dif_series.append(e12 - e26)
        else:
            dif_series.append(None)

    valid_dif = [d for d in dif_series if d is not None]
    if len(valid_dif) < 9:
        return None, None, None, "—"

    dea_series = ema_list(valid_dif, 9)
    if not dea_series or len(dea_series) < 2:
        return None, None, None, "—"

    dif = valid_dif[-1]
    dea = dea_series[-1]
    prev_dea = dea_series[-2] if len(dea_series) >= 2 else dea
    prev_dif = valid_dif[-2] if len(valid_dif) >= 2 else dif
    macd_hist = 2 * (dif - dea)

    # Determine MACD status
    if dif > dea:
        if dif > 0:
            status = "MACD多头"
        else:
            status = "MACD低位金叉" if prev_dif <= prev_dea else "MACD反弹"
    else:
        if dif < 0:
            status = "MACD空头"
        else:
            status = "MACD高位死叉" if prev_dif >= prev_dea else "MACD回落"

    return dif, dea, macd_hist, status

def calc_kdj(highs, lows, closes, n=9):
    """Return (K, D, J) for latest day, plus status string."""
    if len(closes) < n:
        return None, None, None, "—"

    # Calculate RSV for latest day
    h_n = max(highs[-n:])
    l_n = min(lows[-n:])
    if h_n == l_n:
        rsv = 50.0
    else:
        rsv = (closes[-1] - l_n) / (h_n - l_n) * 100

    # Simple KDJ: use last 3 days of RSV-like values
    k_vals = []
    d_vals = []
    prev_k = 50.0
    prev_d = 50.0
    for i in range(max(0, len(closes) - 20), len(closes)):
        hh = max(highs[max(0, i - n + 1):i + 1])
        ll = min(lows[max(0, i - n + 1):i + 1])
        if hh == ll:
            rsv_i = 50.0
        else:
            rsv_i = (closes[i] - ll) / (hh - ll) * 100
        prev_k = 2/3 * prev_k + 1/3 * rsv_i
        prev_d = 2/3 * prev_d + 1/3 * prev_k
        k_vals.append(prev_k)
        d_vals.append(prev_d)

    if not k_vals:
        return None, None, None, "—"

    k = k_vals[-1]
    d = d_vals[-1]
    j = 3 * k - 2 * d

    # Determine KDJ status
    if k > 80 and d > 80:
        status = "KDJ超买"
    elif k < 20 and d < 20:
        status = "KDJ超卖"
    elif k > d:
        status = "KDJ多头" if k > 50 else "KDJ低位金叉"
    else:
        status = "KDJ空头" if k < 50 else "KDJ高位死叉"

    return k, d, j, status

def calc_rsi(closes, n=14):
    """Return RSI(n) value and status string."""
    if len(closes) < n + 1:
        return None, "—"

    gains = []
    losses = []
    for i in range(len(closes) - n, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0)
        losses.append(-change if change < 0 else 0)

    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n

    if avg_loss == 0:
        rsi = 100.0
    else:
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)

    if rsi > 80:
        status = "RSI超买"
    elif rsi > 65:
        status = "RSI偏强"
    elif rsi < 20:
        status = "RSI超卖"
    elif rsi < 35:
        status = "RSI偏弱"
    else:
        status = "RSI中性"

    return rsi, status

def detect_pattern(klines):
    """Detect candlestick pattern. Returns short description string or None."""
    if len(klines) < 2:
        return None

    latest = klines[-1]
    o, c, h, l = latest["open"], latest["close"], latest["high"], latest["low"]
    body = abs(c - o)
    upper_shadow = h - max(c, o)
    lower_shadow = min(c, o) - l
    total_range = h - l

    if total_range == 0:
        return None

    # Doji (十字星): body < 10% of range
    if body < total_range * 0.1:
        if upper_shadow < total_range * 0.1:
            return "蜻蜓十字(底)"
        elif lower_shadow < total_range * 0.1:
            return "墓碑十字(顶)"
        return "十字星"

    # Hammer (锤子线): lower shadow >= 2*body, upper shadow small, at downtrend
    if lower_shadow >= 2 * body and upper_shadow < body * 0.5:
        return "锤子线(反转)"

    # Shooting star (射击之星): upper shadow >= 2*body, lower shadow small
    if upper_shadow >= 2 * body and lower_shadow < body * 0.5:
        return "射击之星(见顶)"

    # Bullish engulfing
    prev = klines[-2]
    prev_body = abs(prev["close"] - prev["open"])
    if c > o and prev["close"] < prev["open"] and body > prev_body * 1.2:
        if o <= prev["close"] and c >= prev["open"]:
            return "看涨吞没"

    # Bearish engulfing
    if c < o and prev["close"] > prev["open"] and body > prev_body * 1.2:
        if o >= prev["close"] and c <= prev["open"]:
            return "看跌吞没"

    return None

# ===================== FUND FLOW =====================

def fetch_fundamental_eastmoney(code):
    """Fetch fundamental + fund flow data from East Money (one call per stock).

    Returns dict with: pe, pb, total_mv, main_flow (主力净流入万元), turnover (换手率)
    Empty dict on failure.
    """
    raw_code = code.replace("sz", "").replace("sh", "")
    if raw_code.startswith(("15", "16", "51", "58", "59")):
        divisor = 1000.0
    else:
        divisor = 100.0

    # Only fetch for stocks (ETFs don't have PE/PB/main_flow in the same way)
    if raw_code.startswith(("15", "16", "51", "58", "59")):
        return {}  # Skip for ETFs

    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": f"0.{raw_code}",
            "fields": "f43,f46,f57,f58,f60,f62,f64,f66,f116,f117,f162,f167,f170",
            "ut": "fa5fd1943c7b386f172d6893dbfdc312",
        }
        resp = http_get(url, params=params, referer="https://quote.eastmoney.com/", timeout=10)
        d = resp.json().get("data", {})
        if not d:
            return {}

        # Main force net inflow (万元)
        main_inflow = float(d.get("f62", 0) or 0) / 10000.0 if d.get("f62") else 0
        super_large = float(d.get("f64", 0) or 0) / 10000.0 if d.get("f64") else 0
        large = float(d.get("f66", 0) or 0) / 10000.0 if d.get("f66") else 0

        pe_raw = float(d.get("f162", 0) or 0)
        pe = pe_raw / 100.0 if pe_raw and pe_raw > 0 else 0

        total_mv = float(d.get("f116", 0) or 0)
        turnover = float(d.get("f167", 0) or 0) / 100.0 if d.get("f167") else 0

        return {
            "pe": pe,
            "total_mv": total_mv,
            "turnover": turnover,
            "main_inflow": main_inflow,
            "super_large": super_large,
            "large": large,
        }
    except Exception:
        return {}

# ===================== ANALYSIS =====================

def analyze(code, name, stype, rt=None, fund=None):
    """Run full analysis pipeline for one stock."""
    klines = fetch_kline_tencent(code, days=60)

    if len(klines) < 20:
        return f"> ❌ 数据不足(仅{len(klines)}天)\n"

    closes = [k["close"] for k in klines]
    highs  = [k["high"] for k in klines]
    lows   = [k["low"] for k in klines]
    vols   = [k["volume"] for k in klines]
    opens  = [k["open"] for k in klines]

    # ---- Price ----
    if rt and rt.get("price", 0) > 0:
        price     = rt["price"]
        chg_pct   = rt["change_pct"]
        prev_close = closes[-1]
        amplitude = (rt["high"] - rt["low"]) / prev_close * 100 if prev_close > 0 else 0
    else:
        price     = closes[-1]
        chg_pct   = klines[-1]["change_pct"]
        amplitude = (klines[-1]["high"] - klines[-1]["low"]) / closes[-2] * 100 if len(closes) >= 2 else 0

    # ---- MA ----
    ma20 = ma(closes, 20)
    vs20 = (price / ma20 - 1) * 100 if ma20 else 0

    # ---- Volume ----
    avg_vol_5  = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0
    if avg_vol_5 > avg_vol_20 * 1.2:
        vol_trend = "放量"
    elif avg_vol_5 < avg_vol_20 * 0.8:
        vol_trend = "缩量"
    else:
        vol_trend = "持平"

    # ---- Consecutive ----
    up_days = down_days = 0
    for i in range(len(klines) - 1, max(0, len(klines) - 9) - 1, -1):
        c = klines[i]["change_pct"]
        if c > 0 and down_days == 0:
            up_days += 1
        elif c < 0 and up_days == 0:
            down_days += 1
        else:
            break
    con_days = up_days or down_days
    con_dir  = "连涨" if up_days else ("连跌" if down_days else "")

    # ---- Returns ----
    r5d  = (closes[-1] / closes[-5]  - 1) * 100 if len(closes) >= 5  else 0
    r10d = (closes[-1] / closes[-10] - 1) * 100 if len(closes) >= 10 else 0
    r20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0

    # ---- Range position ----
    h20 = max(highs[-20:])
    l20 = min(lows[-20:])
    pos20 = (price - l20) / (h20 - l20) * 100 if h20 != l20 else 50

    # ====== Technical: MACD / KDJ / RSI ======
    _, _, _, macd_status = calc_macd(closes)
    k, d, j, kdj_status = calc_kdj(highs, lows, closes)
    rsi_val, rsi_status = calc_rsi(closes)
    pattern = detect_pattern(klines)

    tech_parts = [macd_status, kdj_status, f"RSI{rsi_val:.0f}" if rsi_val else "RSI—"]
    if pattern:
        tech_parts.append(pattern)
    tech_line = " | ".join(tech_parts)

    # ====== Fundamental ======
    fund = fund or {}
    fund_parts = []
    if fund.get("pe", 0) > 0:
        fund_parts.append(f"PE{fund['pe']:.1f}")
    if fund.get("total_mv", 0) > 0:
        mv = fund["total_mv"]
        fund_parts.append(f"市值{mv/1e8:.0f}亿" if mv >= 1e8 else f"市值{mv/1e4:.0f}万")
    if stype == "ETF":
        # For ETFs, show index tracking info
        if "纳指" in name or "纳斯达克" in name:
            fund_parts.append("跟踪纳斯达克100")
        elif "电网" in name:
            fund_parts.append("跟踪电网设备指数")
        elif "芯片" in name:
            fund_parts.append("跟踪国证芯片指数")
    if not fund_parts:
        fund_parts.append("—")
    fund_line = " | ".join(fund_parts)

    # ====== Capital Flow ======
    flow_parts = []
    if fund.get("main_inflow"):
        mi = fund["main_inflow"]
        direction = "流入" if mi > 0 else "流出"
        flow_parts.append(f"主力{direction}{abs(mi):.0f}万")
    if fund.get("turnover", 0) > 0:
        flow_parts.append(f"换手{fund['turnover']:.1f}%")
    elif stype != "ETF":
        flow_parts.append(f"换手—")
    if stype == "ETF":
        flow_parts.append("ETF无主力数据")
    if not flow_parts:
        flow_parts.append("—")
    flow_line = " | ".join(flow_parts)

    # ====== News / Events ======
    news_line = "—"
    if pos20 > 90:
        news_line = "近20日新高，关注突破有效性"
    elif pos20 < 10:
        news_line = "近20日新低，关注止跌信号"
    if abs(r5d) > 10:
        news_line = f"近5日波动{r5d:+.0f}%，注意消息面异动"

    # ====== Scoring ======
    score = 50

    # MA alignment
    ma5  = ma(closes, 5)
    ma10 = ma(closes, 10)
    if all([ma5, ma10, ma20, ma(closes, 60)]):
        ma60v = ma(closes, 60)
        if price > ma5 > ma10 > ma20 > ma60v:
            score += 18
        elif price > ma5 > ma10:
            score += 10
        elif price < ma5 < ma10 < ma20 < ma60v:
            score -= 18
        elif price < ma5 < ma10:
            score -= 10

    # Mean reversion
    if vs20 > 15:
        score -= 4
    elif vs20 > 8:
        score -= 2
    elif vs20 < -15:
        score += 4
    elif vs20 < -8:
        score += 2

    # Volume
    if avg_vol_5 > avg_vol_20 * 1.5 and chg_pct > 1:
        score += 6
    elif avg_vol_5 > avg_vol_20 * 1.5 and chg_pct < -1:
        score -= 6

    # Momentum
    if r5d > 5:
        score += 4
    elif r5d > 2:
        score += 2
    elif r5d < -5:
        score -= 4
    elif r5d < -2:
        score -= 2

    # Range
    if pos20 < 25:
        score += 6
    elif pos20 > 75:
        score -= 6

    # MACD/KDJ
    if "金叉" in macd_status or "金叉" in kdj_status:
        score += 4
    if "死叉" in macd_status or "死叉" in kdj_status:
        score -= 4

    # RSI
    if rsi_val and rsi_val < 30:
        score += 3
    elif rsi_val and rsi_val > 70:
        score -= 3

    # Fund flow bonus
    if fund.get("main_inflow", 0) > 500:
        score += 3
    elif fund.get("main_inflow", 0) < -500:
        score -= 3

    score = max(0, min(100, score))

    if score >= 72:
        sug = ("info", "🟢 **强势** — 可持有或逢回调加仓")
    elif score >= 58:
        sug = ("info", "🟡 **偏多** — 持有为主")
    elif score >= 42:
        sug = ("comment", "⚪ **震荡** — 观望等待方向")
    elif score >= 28:
        sug = ("warning", "🟡 **偏空** — 控制仓位")
    else:
        sug = ("warning", "🔴 **弱势** — 建议减仓")

    # ====== Build Report ======
    arrow = "↑" if chg_pct > 0 else ("↓" if chg_pct < 0 else "→")
    face = "🔴" if chg_pct > 0 else ("🟢" if chg_pct < 0 else "⚪")

    rpt = f"""**{name}** {code} · {stype}
现 **{price:.3f}** {face}{chg_pct:+.2f}% {arrow} | 振{amplitude:.1f}% | 量{vol_trend} | 20日{r20d:+.1f}%
基本面: {fund_line}
技术面: {tech_line}
资金面: {flow_line}
消息面: {news_line}"""

    color, text = sug
    rpt += f"\n<font color=\"{color}\">▶ {text} (评分{score})</font>"

    return rpt


# ==================== WECHAT ====================

def send_wechat(content):
    """推送Markdown至企业微信，自动分段处理4096字节限制."""
    max_bytes = 3900

    if len(content.encode("utf-8")) <= max_bytes:
        return _do_send(content)

    sections = content.split("\n---\n")
    for i, sec in enumerate(sections):
        tag = f"({i + 1}/{len(sections)}) " if len(sections) > 1 else ""
        if i == 0:
            _do_send(sec)
        else:
            _do_send(f"# 📊 (续) {tag}\n{sec}")

def _do_send(content):
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        result = r.json()
        if result.get("errcode") != 0:
            print(f"  [WARN] WeChat: {result}")
        return result
    except Exception as e:
        print(f"  [ERROR] WeChat send failed: {e}")
        return None


# ==================== UTILS ====================

def market_status():
    now = datetime.now()
    if now.weekday() >= 5:
        return "休市(周末)"

    t = now.time()
    if t < time(9, 15):
        return "盘前"
    elif t < time(9, 30):
        return "集合竞价"
    elif t < time(11, 30):
        return "交易中·上午"
    elif t < time(13, 0):
        return "午间休市"
    elif t < time(15, 0):
        return "交易中·下午"
    else:
        return "已收盘"


# ==================== MAIN ====================

def main():
    today = datetime.now()
    WDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wday = WDAYS[today.weekday()]
    mstat = market_status()

    print(f"\n{'=' * 56}")
    print(f"  A股持仓分析  |  {today.strftime('%Y-%m-%d')} {wday}  |  {mstat}")
    print(f"{'=' * 56}\n")

    all_codes = list(STOCKS.keys())
    print(f"[实时] 批量获取 {len(all_codes)} 只股票实时行情...")
    rt_data = fetch_realtime_sina(all_codes)
    print(f"[实时] 获取到 {len(rt_data)} 只\n")

    header = f"""# 📈 A股持仓日报
> {today.strftime('%Y-%m-%d')} {wday} · {mstat}

"""
    # Pre-fetch fund data for stocks (skip ETFs, East Money only for individual stocks)
    fund_data = {}
    for code, info in STOCKS.items():
        if info["type"] == "股票":
            print(f"[资金] 获取 {info['name']} 资金流向...")
            fund_data[code] = fetch_fundamental_eastmoney(code)
            _time.sleep(random.uniform(1.0, 2.0))

    body = ""
    stock_list = list(STOCKS.items())
    for i, (code, info) in enumerate(stock_list):
        print(f"[分析] {info['name']} ({code}) ...")
        try:
            rt = rt_data.get(code)
            fd = fund_data.get(code, {})
            sec = analyze(code, info["name"], info["type"], rt, fd)
            body += sec + "\n\n"
        except Exception as e:
            print(f"  [ERROR] {info['name']} 分析失败: {e}")
            body += f"**{info['name']}** {code}\n> ❌ 数据获取失败\n\n"
        if i < len(stock_list) - 1:
            _time.sleep(random.uniform(0.5, 1.5))

    ts = today.strftime('%Y-%m-%d %H:%M')
    footer = f"> ⚠️ 以上分析由算法自动生成，仅供参考，**不构成投资建议**\n> 🤖 {ts}\n"

    full_report = header + body + footer

    print(f"\n{'=' * 56}")
    print(full_report)
    print(f"{'=' * 56}")

    print("\n📤 推送到企业微信 ...")
    send_wechat(full_report)
    print("✅ 推送完成")


if __name__ == "__main__":
    main()
