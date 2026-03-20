# =====================================================
# PRO++ v2：全异步 Tick级引擎（最终稳定版）
# =====================================================

import asyncio
import time
import pandas as pd
from collections import deque, defaultdict
from datetime import datetime
import requests

from binance import AsyncClient, BinanceSocketManager

# =====================================================
# 参数
# =====================================================
API_KEY = ""
API_SECRET = ""
SERVER_CHAN_KEY = ""

MIN_24H_VOLUME = 8_000_000

PHASE_THRESH_PCT = 0.6
VOL_RATIO_THRESHOLD = 1.5

PHASE_COOLDOWN = 30        # 短冷却
PHASE_WINDOW = 180         # 3分钟窗口

KLINE_MAX = 200

# =====================================================
# 全局数据结构
# =====================================================
kline_1m = defaultdict(lambda: deque(maxlen=KLINE_MAX))
kline_5m = defaultdict(lambda: deque(maxlen=KLINE_MAX))
kline_15m = defaultdict(lambda: deque(maxlen=KLINE_MAX))

current_kline = {}

last_push_time = {}
signal_window = defaultdict(deque)
last_signal_kline = {}

signal_queue = asyncio.PriorityQueue()

# =====================================================
# 推送
# =====================================================
def send_server_chan(title, content):
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    try:
        requests.post(url, data={"title": title, "desp": content}, timeout=5)
    except:
        pass

# =====================================================
# Tick → 1m K线
# =====================================================
def update_kline(symbol, price, qty, ts):
    minute = ts // 60000

    if symbol not in current_kline:
        current_kline[symbol] = {
            "minute": minute,
            "o": price, "h": price, "l": price, "c": price, "v": qty
        }
        return False

    k = current_kline[symbol]

    if minute == k["minute"]:
        k["h"] = max(k["h"], price)
        k["l"] = min(k["l"], price)
        k["c"] = price
        k["v"] += qty
        return False
    else:
        # 完成K线
        kline_1m[symbol].append(k.copy())
        aggregate_mtf(symbol)

        # 新K线
        current_kline[symbol] = {
            "minute": minute,
            "o": price, "h": price, "l": price, "c": price, "v": qty
        }
        return True

# =====================================================
# 多周期聚合
# =====================================================
def aggregate_mtf(symbol):
    data = list(kline_1m[symbol])
    if len(data) < 15:
        return

    df = pd.DataFrame(data)

    df5 = df.groupby(df.index // 5).agg({
        'o': 'first','h': 'max','l': 'min','c': 'last','v': 'sum'
    })
    kline_5m[symbol] = deque(df5.to_dict("records"), maxlen=KLINE_MAX)

    df15 = df.groupby(df.index // 15).agg({
        'o': 'first','h': 'max','l': 'min','c': 'last','v': 'sum'
    })
    kline_15m[symbol] = deque(df15.to_dict("records"), maxlen=KLINE_MAX)

# =====================================================
# EMA趋势
# =====================================================
def ema_trend(df):
    if len(df) < 20:
        return "未知"
    ema = df['c'].ewm(span=144).mean()
    if df['c'].iloc[-1] > ema.iloc[-1]:
        return "看涨"
    elif df['c'].iloc[-1] < ema.iloc[-1]:
        return "看跌"
    return "震荡"

# =====================================================
# 评分
# =====================================================
def calc_score(symbol):
    if len(kline_1m[symbol]) < 20:
        return None

    df = pd.DataFrame(kline_1m[symbol])
    last = df.iloc[-1]

    pct = (last['c'] - last['o']) / last['o'] * 100
    vol_ma = df['v'].rolling(20).mean().iloc[-1]
    vol_ratio = last['v'] / (vol_ma + 1e-6)

    score = 0
    if vol_ratio > 2: score += 2
    if abs(pct) > 1: score += 2

    trend_1m = ema_trend(df)
    trend_5m = ema_trend(pd.DataFrame(kline_5m[symbol])) if kline_5m[symbol] else "未知"
    trend_15m = ema_trend(pd.DataFrame(kline_15m[symbol])) if kline_15m[symbol] else "未知"

    return {
        "symbol": symbol,
        "score": score,
        "pct": pct,
        "vol_ratio": vol_ratio,
        "trend": f"{trend_1m}/{trend_5m}/{trend_15m}"
    }

# =====================================================
# 信号检测（终极过滤版）
# =====================================================
async def detect_signal(symbol):

    res = calc_score(symbol)
    if not res:
        return

    pct = res["pct"]
    vol = res["vol_ratio"]

    if abs(pct) >= PHASE_THRESH_PCT and vol >= VOL_RATIO_THRESHOLD:
        phase = 2
    elif abs(pct) >= PHASE_THRESH_PCT * 1.5:
        phase = 4
    else:
        return

    now = time.time()
    key = (symbol, phase)

    # =========================
    # 同一K线去重
    # =========================
    kline_id = current_kline[symbol]["minute"]
    if last_signal_kline.get(key) == kline_id:
        return
    last_signal_kline[key] = kline_id

    # =========================
    # 冷却过滤
    # =========================
    if key in last_push_time:
        if now - last_push_time[key] < PHASE_COOLDOWN:
            return

    # =========================
    # 3分钟窗口过滤
    # =========================
    window = signal_window[key]

    while window and now - window[0] > PHASE_WINDOW:
        window.popleft()

    if len(window) >= 1:
        return

    window.append(now)
    last_push_time[key] = now

    # =========================
    # 入优先队列
    # =========================
    await signal_queue.put((-res["score"], res, phase))

# =====================================================
# 推送Worker
# =====================================================
async def signal_worker():
    while True:
        score, res, phase = await signal_queue.get()

        msg = f"""
币对: {res['symbol']}
Phase: {phase}
Score: {-score}
涨幅: {res['pct']:.2f}%
放量: {res['vol_ratio']:.2f}
趋势: {res['trend']}
时间: {datetime.now()}
"""
        send_server_chan(f"{res['symbol']} Phase{phase}", msg)

# =====================================================
# WS分片
# =====================================================
async def run_ws_shard(client, symbols):
    bm = BinanceSocketManager(client)
    streams = [f"{s.lower()}@aggTrade" for s in symbols]

    ms = bm.multiplex_socket(streams)

    async with ms as stream:
        while True:
            try:
                msg = await stream.recv()
                data = msg.get("data", {})

                symbol = data.get("s")
                price = float(data.get("p", 0))
                qty = float(data.get("q", 0))
                ts = data.get("T", 0)

                new_kline = update_kline(symbol, price, qty, ts)

                # 只在新K线完成后检测（极大降噪）
                if new_kline:
                    await detect_signal(symbol)

            except Exception as e:
                print("WS异常:", e)
                await asyncio.sleep(1)

# =====================================================
# 主函数
# =====================================================
async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET)

    tickers = await client.futures_ticker()
    symbols = [
        t['symbol'] for t in tickers
        if t['symbol'].endswith("USDT") and float(t['quoteVolume']) > MIN_24H_VOLUME
    ]

    print(f"交易对数量: {len(symbols)}")

    shard_size = 15
    shards = [symbols[i:i + shard_size] for i in range(0, len(symbols), shard_size)]

    tasks = []

    for shard in shards:
        tasks.append(run_ws_shard(client, shard))

    tasks.append(signal_worker())

    await asyncio.gather(*tasks)

# =====================================================
# 启动
# =====================================================
if __name__ == "__main__":
    print("🚀 PRO++ v2 启动（Tick级 + 去重 + 分片 + 队列）")
    asyncio.run(main())
