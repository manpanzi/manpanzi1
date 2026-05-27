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

# ===================== ANALYSIS =====================

def analyze(code, name, stype, rt=None):
    """Run full analysis pipeline for one stock."""
    klines = fetch_kline_tencent(code, days=60)

    if len(klines) < 20:
        return f"> ❌ 数据不足(仅{len(klines)}天)\n"

    closes = [k["close"] for k in klines]
    highs  = [k["high"] for k in klines]
    lows   = [k["low"] for k in klines]
    vols   = [k["volume"] for k in klines]

    if rt and rt.get("price", 0) > 0:
        price     = rt["price"]
        chg_pct   = rt["change_pct"]
        t_open    = rt["open"]
        t_high    = rt["high"]
        t_low     = rt["low"]
        vol_ratio = rt.get("vol_ratio", 1.0)
        prev_close = closes[-1]
        amplitude = (t_high - t_low) / prev_close * 100 if prev_close > 0 else 0
    else:
        price     = closes[-1]
        chg_pct   = klines[-1]["change_pct"]
        t_open    = klines[-1]["open"]
        t_high    = klines[-1]["high"]
        t_low     = klines[-1]["low"]
        amplitude = klines[-1]["amplitude"]
        vol_ratio = 1.0

    ma5  = ma(closes, 5)
    ma10 = ma(closes, 10)
    ma20 = ma(closes, 20)
    ma60 = ma(closes, 60)

    vs5  = (price / ma5  - 1) * 100 if ma5 else 0
    vs10 = (price / ma10 - 1) * 100 if ma10 else 0
    vs20 = (price / ma20 - 1) * 100 if ma20 else 0
    vs60 = (price / ma60 - 1) * 100 if ma60 else 0

    r5d  = (closes[-1] / closes[-5]  - 1) * 100 if len(closes) >= 5  else 0
    r10d = (closes[-1] / closes[-10] - 1) * 100 if len(closes) >= 10 else 0
    r20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0

    avg_vol_5  = sum(vols[-5:]) / 5
    avg_vol_20 = sum(vols[-20:]) / 20
    if avg_vol_5 > avg_vol_20 * 1.2:
        vol_trend = "放量"
    elif avg_vol_5 < avg_vol_20 * 0.8:
        vol_trend = "缩量"
    else:
        vol_trend = "持平"

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

    h20  = max(highs[-20:])
    l20  = min(lows[-20:])
    pos20 = (price - l20) / (h20 - l20) * 100 if h20 != l20 else 50

    # ---- Signals ----
    sigs = []

    if ma5 and ma10 and len(closes) >= 11:
        p_ma5  = ma(closes[:-1], 5)
        p_ma10 = ma(closes[:-1], 10)
        if p_ma5 and p_ma10:
            if p_ma5 <= p_ma10 and ma5 > ma10:
                sigs.append(("golden", "MA5↑上穿MA10，金叉看涨"))
            elif p_ma5 >= p_ma10 and ma5 < ma10:
                sigs.append(("death", "MA5↓下穿MA10，死叉看跌"))

    if all([ma5, ma10, ma20, ma60]):
        if price > ma5 > ma10 > ma20 > ma60:
            sigs.append(("bull_align", "均线完全多头排列，强势"))
        elif price < ma5 < ma10 < ma20 < ma60:
            sigs.append(("bear_align", "均线完全空头排列，弱势"))
        elif price > ma5 > ma10:
            sigs.append(("short_bull", "短期均线多头排列"))
        elif price < ma5 < ma10:
            sigs.append(("short_bear", "短期均线空头排列"))

    if vs20 > 20:
        sigs.append(("overbought", f"价格偏离20日均线{vs20:.0f}%，短线超买"))
    elif vs20 < -20:
        sigs.append(("oversold", f"价格偏离20日均线{vs20:.0f}%，短线超卖"))

    if vol_ratio > 2.0:
        sigs.append(("vol_anomaly", f"量比{vol_ratio:.1f}，异常放量"))
    elif vol_ratio > 1.5:
        sigs.append(("vol_up", f"量比{vol_ratio:.1f}，温和放量"))
    elif vol_ratio < 0.5:
        sigs.append(("vol_down", f"量比{vol_ratio:.1f}，极度缩量"))

    if up_days >= 4:
        sigs.append(("consec_up", f"连涨{up_days}日，追高需谨慎"))
    elif up_days >= 3:
        sigs.append(("consec_up_mild", f"连涨{up_days}日，趋势偏强"))
    if down_days >= 4:
        sigs.append(("consec_down", f"连跌{down_days}日，关注超跌反弹"))
    elif down_days >= 3:
        sigs.append(("consec_down_mild", f"连跌{down_days}日，趋势偏弱"))

    if pos20 > 85:
        sigs.append(("near_high", "接近20日高点，上方压力区"))
    elif pos20 < 15:
        sigs.append(("near_low", "接近20日低点，下方支撑区"))

    # ---- Scoring ----
    score = 50

    if all([ma5, ma10, ma20, ma60]):
        if price > ma5 > ma10 > ma20 > ma60:
            score += 18
        elif price > ma5 > ma10:
            score += 10
        elif price < ma5 < ma10 < ma20 < ma60:
            score -= 18
        elif price < ma5 < ma10:
            score -= 10

    if vs20 > 15:
        score -= 4
    elif vs20 > 8:
        score -= 2
    elif vs20 < -15:
        score += 4
    elif vs20 < -8:
        score += 2

    if vol_ratio > 1.5 and chg_pct > 1:
        score += 6
    elif vol_ratio > 1.5 and chg_pct < -1:
        score -= 6

    if r5d > 5:
        score += 4
    elif r5d > 2:
        score += 2
    elif r5d < -5:
        score -= 4
    elif r5d < -2:
        score -= 2

    if pos20 < 25:
        score += 6
    elif pos20 > 75:
        score -= 6

    for t, _ in sigs:
        if t == "golden":
            score += 6
        elif t == "death":
            score -= 6

    score = max(0, min(100, score))

    if score >= 72:
        sug = ("info", "🟢 **强势** — 可持有或逢回调加仓")
    elif score >= 58:
        sug = ("info", "🟡 **偏多** — 持有为主，注意上方压力")
    elif score >= 42:
        sug = ("comment", "⚪ **震荡** — 建议观望，等待方向选择")
    elif score >= 28:
        sug = ("warning", "🟡 **偏空** — 控制仓位，反弹可减仓")
    else:
        sug = ("warning", "🔴 **弱势** — 建议轻仓或观望")

    # ---- Build Report ----
    arrow = "↑" if chg_pct > 0 else ("↓" if chg_pct < 0 else "→")
    face = "🔴" if chg_pct > 0 else ("🟢" if chg_pct < 0 else "⚪")

    if all([ma5, ma10, ma20, ma60]):
        if price > ma5 > ma10 > ma20 > ma60:
            ma_status = "均线多头排列"
        elif price < ma5 < ma10 < ma20 < ma60:
            ma_status = "均线空头排列"
        elif price > ma5 > ma10:
            ma_status = "短期多头"
        elif price < ma5 < ma10:
            ma_status = "短期空头"
        else:
            ma_status = "均线缠绕"
    else:
        ma_status = "—"

    trend_parts = [f"5日{r5d:+.1f}%"]
    if abs(r20d) > 2:
        trend_parts.append(f"20日{r20d:+.1f}%")
    if con_dir:
        trend_parts.append(f"{con_dir}{con_days}日")
    trend_line = "  ".join(trend_parts)

    priority_sigs = []
    for t, msg in sigs:
        if t in ("golden", "death", "bull_align", "bear_align"):
            priority_sigs.append(msg)
        elif t in ("overbought", "oversold"):
            priority_sigs.append(msg)
    if len(priority_sigs) < 2:
        for t, msg in sigs:
            if t not in ("golden", "death", "bull_align", "bear_align", "overbought", "oversold"):
                if len(priority_sigs) < 3:
                    priority_sigs.append(msg)

    vol_desc = f"量{vol_trend}"

    rpt = f"""**{name}** {code} · {stype}
现 **{price:.3f}** {face}{chg_pct:+.2f}% {arrow} | 振{amplitude:.1f}% | {vol_desc}
均线: {ma_status} | MA20偏离 {vs20:+.1f}%
趋势: {trend_line}"""

    if priority_sigs:
        rpt += "\n信号: " + "；".join(priority_sigs)

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
    body = ""
    stock_list = list(STOCKS.items())
    for i, (code, info) in enumerate(stock_list):
        print(f"[分析] {info['name']} ({code}) ...")
        try:
            rt = rt_data.get(code)
            sec = analyze(code, info["name"], info["type"], rt)
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
