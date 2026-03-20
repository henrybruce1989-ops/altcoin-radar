# =====================================================
# 全市场 Phase2/Phase4 WebSocket asyncio（最终稳定版）
# 🔧仅修改：24h成交额过滤（>=800万）
# =====================================================
import asyncio, json, csv, os, time
from datetime import datetime, timezone, timedelta
from collections import deque
import aiohttp
import pandas as pd
import requests
from binance.client import Client

# =====================================================
# 参数区
# =====================================================
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "sctp14659thuntd89pzhhlsmbwynooxu")

MIN_24H_VOLUME = 8_000_000   # 🔧修改：强制过滤800万
TOPN = 200

WINDOW_SECONDS = 15
MAX_QUEUE_LEN = 1000
PHASE_COOLDOWN = 300

MAX_CONCURRENT_WS = 30
BATCH_SIZE = 20

Z_THRESHOLD_PHASE2 = 2.0
Z_THRESHOLD_PHASE4 = 2.5
VOL_RATIO_PHASE2 = 1.2
VOL_RATIO_PHASE4 = 1.5
MQ_RATIO_PHASE2 = 1.1
MQ_RATIO_PHASE4 = 1.2
ATR_RATIO_PHASE4 = 1.2

SIGNAL_CSV = "signals_final.csv"

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
            writer.writerow(["time","symbol","phase","pct","z","atr"])
        writer.writerow(data)

# =====================================================
# EMA趋势
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
# Z-score + ATR + 动量
# =====================================================
def calc_advanced_metrics(trades):
    prices = [t['price'] for t in trades]
    if len(prices) < 20:
        return None

    pct_series = pd.Series(prices).pct_change().dropna()*100
    pct = pct_series.iloc[-1]
    z = (pct - pct_series.mean()) / (pct_series.std()+1e-6)

    highs = pd.Series(prices).rolling(3).max()
    lows = pd.Series(prices).rolling(3).min()
    closes = pd.Series(prices)

    tr = pd.concat([
        highs - lows,
        (highs - closes.shift()).abs(),
        (lows - closes.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(10).mean().iloc[-1]
    atr_ratio = (max(prices)-min(prices)) / (atr+1e-6)

    diffs = pd.Series(prices).diff().dropna()
    consecutive_up = (diffs>0).astype(int).groupby((diffs<=0).cumsum()).sum().iloc[-1]

    momentum_up = consecutive_up >= 3

    return pct, z, atr_ratio, momentum_up

# =====================================================
# 推送
# =====================================================
def push(symbol, phase, pct, z, atr):
    t = (datetime.now(timezone.utc)+timedelta(hours=8)).strftime("%H:%M:%S")
    msg = f"{symbol} Phase{phase} pct:{pct:.2f}% z:{z:.2f} atr:{atr:.2f}"
    print(msg)
    send_server_chan(symbol, msg)
    save_csv([t,symbol,phase,pct,z,atr])

# =====================================================
# Phase逻辑
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

            pct, z, atr_ratio, momentum = metrics

            if symbol not in observation_pool:
                observation_pool[symbol] = {"phase":1,"last":0}

            info = observation_pool[symbol]

            if time.time() - info["last"] < PHASE_COOLDOWN:
                continue

            if info["phase"] == 1:
                if z >= Z_THRESHOLD_PHASE2:
                    push(symbol,2,pct,z,atr_ratio)
                    info["phase"]=2
                    info["last"]=time.time()

            elif info["phase"] == 2:
                if z >= Z_THRESHOLD_PHASE4 and atr_ratio >= ATR_RATIO_PHASE4 and momentum:
                    push(symbol,4,pct,z,atr_ratio)
                    info["phase"]=1
                    info["last"]=time.time()

# =====================================================
# WebSocket
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
                                "qty": float(data['q'])
                            })
            except:
                print(f"[重连] {symbol}")
                await asyncio.sleep(5)

# =====================================================
# 🔧修改：核心过滤函数（重点）
# =====================================================
def get_symbols():
    tickers = client.futures_ticker()

    filtered = []

    for t in tickers:
        try:
            symbol = t['symbol']

            if not symbol.endswith("USDT"):
                continue

            quote_volume = float(t['quoteVolume'])

            # 🔧关键过滤：24h成交额 >= 800万
            if quote_volume < MIN_24H_VOLUME:
                continue

            filtered.append({
                "symbol": symbol,
                "quoteVolume": quote_volume
            })

        except:
            continue

    # 🔧关键：按成交额排序
    filtered.sort(key=lambda x: x['quoteVolume'], reverse=True)

    symbols = [x['symbol'] for x in filtered[:TOPN]]

    print(f"✅ 过滤后币种数量: {len(symbols)}")
    print("TOP10:", symbols[:10])

    return symbols

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

if __name__ == "__main__":
    asyncio.run(main())
