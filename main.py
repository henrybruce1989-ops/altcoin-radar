# =====================================================
# FINAL PRO版：Phase2/4 + 全指标 + 评分Top5% + WebSocket稳定版
# =====================================================
import asyncio, json, csv, os, time
from datetime import datetime, timezone, timedelta
from collections import deque
import aiohttp
import pandas as pd
import requests
from binance.client import Client

# =====================================================
# 参数区（全部可调）
# =====================================================
API_KEY = ""
API_SECRET = ""
SERVER_CHAN_KEY = "sctp14659thuntd89pzhhlsmbwynooxu"

MIN_24H_VOLUME = 8_000_000
TOPN = 200

WINDOW_SECONDS = 15
MAX_QUEUE_LEN = 1000
PHASE_COOLDOWN = 300

MAX_CONCURRENT_WS = 30
BATCH_SIZE = 20

Z_THRESHOLD_PHASE2 = 2.0
Z_THRESHOLD_PHASE4 = 2.5

VOL_RATIO_PHASE2 = 1.5
VOL_RATIO_PHASE4 = 2

MQ_RATIO_PHASE2 = 1.5
MQ_RATIO_PHASE4 = 2

ATR_RATIO_PHASE4 = 1.5

SIGNAL_POOL_SIZE = 200
TOP_PERCENT = 0.1

# =====================================================
client = Client(API_KEY, API_SECRET)

trade_queues = {}
observation_pool = {}
signal_pool = []

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
# ⭐评分系统
# =====================================================
def calc_signal_score(z, vol_ratio, mq_ratio, atr_ratio,
                     trend_resonance, momentum):

    score = 0

    if z >= 4: score += 5
    elif z >= 3: score += 4
    elif z >= 2.5: score += 3
    elif z >= 2: score += 2

    if vol_ratio >= 2: score += 3
    elif vol_ratio >= 1.5: score += 2
    elif vol_ratio >= 1.2: score += 1

    if mq_ratio >= 2: score += 3
    elif mq_ratio >= 1.5: score += 2
    elif mq_ratio >= 1.2: score += 1

    if atr_ratio >= 2: score += 3
    elif atr_ratio >= 1.5: score += 2
    elif atr_ratio >= 1.2: score += 1

    if trend_resonance != "震荡":
        score += 2

    if momentum:
        score += 2

    return score

# =====================================================
# 推送（完整字段恢复）
# =====================================================
def push(symbol, phase, pct, z, atr_ratio,
         vol_ratio, range_ratio, mq_ratio,
         speed, compression, accumulation,
         trend_1m, trend_5m, trend_15m,
         trend_resonance, predict_dir):

    t_str = (datetime.now(timezone.utc)+timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')

    msg = f"""
币对: {symbol}
Phase: {phase} {"埋伏" if phase==2 else "开仓"}

涨幅pct: {pct:.2f}%
Z-score: {z:.2f}
ATR强度: {atr_ratio:.2f}

放量比: {vol_ratio:.2f}x
波动比: {range_ratio:.2f}x
资金质量比: {mq_ratio:.2f}

涨幅速度: {speed:.2f}%
压缩/吸筹: {compression} / {accumulation}

趋势: 1m:{trend_1m} 5m:{trend_5m} 15m:{trend_15m}
共振: {trend_resonance}

15秒预测: {predict_dir}

时间(GMT+8): {t_str}
"""

    print(msg)
    send_server_chan(f"{symbol} Phase{phase}", msg)

# =====================================================
# 核心计算
# =====================================================
def calc_metrics(trades, history):

    prices = [t['price'] for t in trades]
    if len(prices) < 20:
        return None

    pct = (prices[-1]-prices[0])/prices[0]*100

    pct_series = pd.Series(prices).pct_change().dropna()*100
    z = (pct_series.iloc[-1] - pct_series.mean())/(pct_series.std()+1e-6)

    atr = (max(prices)-min(prices)) / (pd.Series(prices).diff().abs().mean()+1e-6)
    atr_ratio = atr

    vol_now = sum(t['qty'] for t in trades)
    vol_hist = sum(t['qty'] for t in history[-300:])
    vol_ratio = vol_now/(vol_hist/3+1e-6)

    notional = [t['price']*t['qty'] for t in trades]
    mq_ratio = (sum(notional)/len(notional))/(sum(notional)/len(notional)+1e-6)

    range_ratio = (max(prices)-min(prices)) / (
        (max([t['price'] for t in history[-300:]]) - min([t['price'] for t in history[-300:]]))+1e-6
    )

    speed = pct

    diffs = pd.Series(prices).diff().dropna()
    momentum = (diffs.tail(3)>0).sum()>=3 or (diffs.tail(3)<0).sum()>=3

    compression = "强" if (max(prices)-min(prices)) < 0.5 else "弱"
    accumulation = "强" if vol_now < vol_hist else "弱"

    predict_dir = "↑上涨" if pct>0 else "↓下跌"

    return pct, z, atr_ratio, vol_ratio, range_ratio, mq_ratio, speed, compression, accumulation, momentum, predict_dir

# =====================================================
# Phase监控
# =====================================================
async def phase_monitor(symbol):
    while True:
        await asyncio.sleep(1)

        async with lock:
            if symbol not in trade_queues:
                continue

            trades = list(trade_queues[symbol])[-100:]
            history = list(trade_queues[symbol])

            m = calc_metrics(trades, history)
            if not m:
                continue

            pct,z,atr_ratio,vol_ratio,range_ratio,mq_ratio,speed,compression,accumulation,momentum,predict_dir = m

            trend_1m = get_ema_trend(symbol,"1m")
            trend_5m = get_ema_trend(symbol,"5m")
            trend_15m = get_ema_trend(symbol,"15m")

            trends=[trend_1m,trend_5m,trend_15m]
            if trends.count("看涨")>=2:
                trend_resonance="看涨共振"
            elif trends.count("看跌")>=2:
                trend_resonance="看跌共振"
            else:
                trend_resonance="震荡"

            score = calc_signal_score(z,vol_ratio,mq_ratio,atr_ratio,trend_resonance,momentum)

            signal = (symbol,2,pct,z,atr_ratio,vol_ratio,range_ratio,mq_ratio,
                      speed,compression,accumulation,
                      trend_1m,trend_5m,trend_15m,trend_resonance,predict_dir)

            signal_pool.append({"score":score,"data":signal})
            if len(signal_pool)>SIGNAL_POOL_SIZE:
                signal_pool.pop(0)

            sorted_pool = sorted(signal_pool,key=lambda x:x["score"],reverse=True)
            top_n = max(1,int(len(sorted_pool)*TOP_PERCENT))

            if {"score":score,"data":signal} in sorted_pool[:top_n]:
                push(*signal)

# =====================================================
# WebSocket订阅
# =====================================================
async def subscribe(symbol):
    url=f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"

    async with semaphore:
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(url) as ws:
                        async for msg in ws:
                            data=json.loads(msg.data)

                            if symbol not in trade_queues:
                                trade_queues[symbol]=deque(maxlen=MAX_QUEUE_LEN)

                            trade_queues[symbol].append({
                                "price":float(data['p']),
                                "qty":float(data['q'])
                            })
            except:
                await asyncio.sleep(5)

# =====================================================
# 币种过滤（800万）
# =====================================================
def get_symbols():
    tickers=client.futures_ticker()

    filtered=[t for t in tickers if t['symbol'].endswith("USDT")
              and float(t['quoteVolume'])>=MIN_24H_VOLUME]

    filtered.sort(key=lambda x:float(x['quoteVolume']),reverse=True)

    symbols=[t['symbol'] for t in filtered[:TOPN]]

    print("监听币种:",len(symbols))
    return symbols

# =====================================================
# 主启动
# =====================================================
async def main():
    symbols=get_symbols()
    tasks=[]

    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]

        for s in batch:
            tasks.append(asyncio.create_task(subscribe(s)))
            tasks.append(asyncio.create_task(phase_monitor(s)))

        await asyncio.sleep(1)

    await asyncio.gather(*tasks)

if __name__=="__main__":
    asyncio.run(main())
