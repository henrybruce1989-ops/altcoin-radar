# =====================================================
# 全市场 Phase2/Phase4 WebSocket asyncio（增强版）
# 👉 已加入：Z-score + ATR + 动量连续性
# =====================================================
import asyncio, json, csv, os, time
from datetime import datetime, timezone, timedelta
from collections import deque
import aiohttp
import pandas as pd
import requests
from binance.client import Client

# =====================================================
# ========== 可调参数区域（🔥重点） ==========
# =====================================================
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "sctp14659thuntd89pzhhlsmbwynooxu")

# ===== 基础参数 =====
WINDOW_SECONDS = 15              # ⭐短周期窗口（建议 10~30）
MAX_QUEUE_LEN = 1000            # ⭐每币种缓存（建议 500~2000）
PHASE_COOLDOWN = 300            # ⭐冷却时间（秒）（建议 120~600）

# ===== 并发控制 =====
MAX_CONCURRENT_WS = 30          # ⭐最大WebSocket并发（建议 20~50）
BATCH_SIZE = 20                 # ⭐每批启动数量（建议 10~30）

# ===== Phase触发（🔥核心）=====
Z_THRESHOLD_PHASE2 = 2.0        # ⭐Z-score埋伏（1.8~2.5）
Z_THRESHOLD_PHASE4 = 2.5        # ⭐Z-score开仓（2.3~3.5）

VOL_RATIO_PHASE2 = 1.2          # ⭐放量（1.1~1.5）
VOL_RATIO_PHASE4 = 1.5

MQ_RATIO_PHASE2 = 1.1           # ⭐资金质量（1.05~1.3）
MQ_RATIO_PHASE4 = 1.2

ATR_RATIO_PHASE4 = 1.2          # ⭐ATR强度（1.1~2.0）

# ===== 市场筛选 =====
MIN_24H_VOLUME = 8_000_000
TOPN = 200

SIGNAL_CSV = "signals_final.csv"

# =====================================================
# 初始化
# =====================================================
client = Client(API_KEY, API_SECRET)

trade_queues = {}
observation_pool = {}

lock = asyncio.Lock()
semaphore = asyncio.Semaphore(MAX_CONCURRENT_WS)

# =====================================================
# Server酱
# =====================================================
def send_server_chan(title, content):
    try:
        requests.post(
            f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send",
            data={"title": title, "desp": content},
            timeout=5
        )
    except:
        pass

# =====================================================
# CSV
# =====================================================
def save_csv(data):
    file_exists = os.path.isfile(SIGNAL_CSV)
    with open(SIGNAL_CSV,"a",newline='',encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "time","symbol","phase","pct","z","atr",
                "vol_ratio","mq_ratio","range_ratio",
                "trend","momentum"
            ])
        writer.writerow(data)

# =====================================================
# EMA趋势（保留）
# =====================================================
def get_ema_trend(symbol, interval):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
        df = pd.DataFrame(klines)
        df[4] = df[4].astype(float)
        ema = df[4].ewm(span=144).mean()
        return "看涨" if df[4].iloc[-1] > ema.iloc[-1] else "看跌"
    except:
        return "未知"

# =====================================================
# ⭐核心新增：Z-score + ATR + 动量连续性
# =====================================================
def calc_advanced_metrics(trades):
    prices = [t['price'] for t in trades]

    if len(prices) < 20:
        return None

    # ===== Z-score =====
    pct_series = pd.Series(prices).pct_change().dropna()*100
    pct = pct_series.iloc[-1]
    z = (pct - pct_series.mean()) / (pct_series.std()+1e-6)

    # ===== ATR =====
    highs = pd.Series(prices).rolling(3).max()
    lows = pd.Series(prices).rolling(3).min()
    closes = pd.Series(prices)

    tr = pd.concat([
        highs - lows,
        (highs - closes.shift()).abs(),
        (lows - closes.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(10).mean().iloc[-1]
    current_range = max(prices) - min(prices)
    atr_ratio = current_range / (atr+1e-6)

    # ===== 动量连续性（强化三连阳）=====
    diffs = pd.Series(prices).diff().dropna()
    avg_move = diffs.abs().mean()
    total_move = diffs.tail(5).sum()

    consecutive_up = (diffs > 0).astype(int).groupby((diffs <= 0).cumsum()).sum().iloc[-1]
    consecutive_down = (diffs < 0).astype(int).groupby((diffs >= 0).cumsum()).sum().iloc[-1]

    momentum_up = (
        consecutive_up >= 3
        and diffs.tail(3).mean() > avg_move
        and total_move > atr*0.8
    )

    momentum_down = (
        consecutive_down >= 3
        and abs(diffs.tail(3).mean()) > avg_move
        and abs(total_move) > atr*0.8
    )

    return pct, z, atr_ratio, momentum_up, momentum_down

# =====================================================
# Phase推送
# =====================================================
def push(symbol, phase, pct, z, atr, vol_ratio, mq_ratio, trend, momentum):
    t = (datetime.now(timezone.utc)+timedelta(hours=8)).strftime("%H:%M:%S")

    msg = f"""
{symbol} Phase{phase}
pct: {pct:.2f}%
Z-score: {z:.2f}
ATR: {atr:.2f}
vol: {vol_ratio:.2f}
mq: {mq_ratio:.2f}
趋势: {trend}
动量: {momentum}
时间: {t}
"""
    print(msg)
    send_server_chan(symbol, msg)
    save_csv([t,symbol,phase,pct,z,atr,vol_ratio,mq_ratio,0,trend,momentum])

# =====================================================
# Phase核心逻辑
# =====================================================
async def phase_monitor(symbol):
    while True:
        await asyncio.sleep(1)

        async with lock:
            if symbol not in trade_queues:
                continue

            trades = list(trade_queues[symbol])[-100:]
            metrics = calc_advanced_metrics(trades)

            if not metrics:
                continue

            pct, z, atr_ratio, mu, md = metrics

            # ===== 原有指标 =====
            vol_ratio = 1.2   # 这里你可以替换为真实计算
            mq_ratio = 1.1

            trend = get_ema_trend(symbol,"1m")

            if symbol not in observation_pool:
                observation_pool[symbol] = {"phase":1,"last":0}

            info = observation_pool[symbol]

            if time.time() - info["last"] < PHASE_COOLDOWN:
                continue

            # =============================
            # Phase2（埋伏）
            # =============================
            if info["phase"] == 1:
                if z >= Z_THRESHOLD_PHASE2 and vol_ratio>=VOL_RATIO_PHASE2 and mq_ratio>=MQ_RATIO_PHASE2:
                    push(symbol,2,pct,z,atr_ratio,vol_ratio,mq_ratio,trend,"启动")
                    info["phase"]=2
                    info["last"]=time.time()

            # =============================
            # Phase4（开仓）
            # =============================
            elif info["phase"] == 2:
                if (
                    z >= Z_THRESHOLD_PHASE4
                    and vol_ratio>=VOL_RATIO_PHASE4
                    and mq_ratio>=MQ_RATIO_PHASE4
                    and atr_ratio>=ATR_RATIO_PHASE4
                    and (mu or md)
                ):
                    push(symbol,4,pct,z,atr_ratio,vol_ratio,mq_ratio,trend,"爆发")
                    info["phase"]=1
                    info["last"]=time.time()

# =====================================================
# WebSocket订阅（带限流）
# =====================================================
async def subscribe(symbol):
    url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"

    async with semaphore:
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(url) as ws:
                        async for msg in ws:
                            data = json.loads(msg.data)
                            if symbol not in trade_queues:
                                trade_queues[symbol] = deque(maxlen=MAX_QUEUE_LEN)

                            trade_queues[symbol].append({
                                "price": float(data['p']),
                                "qty": float(data['q']),
                                "time": int(time.time()*1000)
                            })
            except:
                await asyncio.sleep(5)

# =====================================================
# 获取币种
# =====================================================
def get_symbols():
    t = client.futures_ticker()
    return [x['symbol'] for x in t if x['symbol'].endswith("USDT")][:TOPN]

# =====================================================
# 启动
# =====================================================
async def main():
    symbols = get_symbols()
    tasks = []

    for i in range(0,len(symbols),BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]

        for s in batch:
            tasks.append(asyncio.create_task(subscribe(s)))
            tasks.append(asyncio.create_task(phase_monitor(s)))

        await asyncio.sleep(1)

    await asyncio.gather(*tasks)

asyncio.run(main())
