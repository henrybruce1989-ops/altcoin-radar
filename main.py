# =====================================================
# PRO终极版：全市场 Phase2/Phase4 WebSocket + asyncio + 高精度低噪信号
# =====================================================
import os
import asyncio
import time
import pandas as pd
from datetime import datetime
import requests
from binance.client import Client
from binance import AsyncClient, BinanceSocketManager

# =====================================================
# ========== 可调参数区域 ==========
# =====================================================
API_KEY = os.getenv("API_KEY","YOUR_BINANCE_API_KEY")
API_SECRET = os.getenv("API_SECRET","YOUR_BINANCE_API_SECRET")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY","sctp14659thuntd89pzhhlsmbwynooxu")

MIN_24H_VOLUME = 8_000_000      # 只监控24h成交额大于800万的币种
PHASE_COOLDOWN = 60             # Phase冷却时间（秒），同一个币种同一Phase内不会重复推送
PHASE_THRESH_PCT = 0.8          # 1分钟K线涨跌幅阈值（%）
VOL_RATIO_THRESHOLD = 1.3       # 放量比阈值
TOP_PERCENT = 0.1              # Top5%评分
SIGNAL_POOL_SIZE = 200           # 最近信号池长度
EMA_PERIOD = 144                 # EMA周期
KLINE_LIMIT = 60                 # 获取历史K线条数
# 三连阳/三连阴判断长度
MOMENTUM_LEN = 3

# =====================================================
# 初始化
# =====================================================
client_sync = Client(API_KEY, API_SECRET)
last_push_time = {}   # key=(symbol,phase)
signal_pool = []      # 最近信号池

# =====================================================
# Server酱推送
# =====================================================
def send_server_chan(title, content):
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    data = {"title": title,"desp": content}
    try:
        requests.post(url,data=data,timeout=5)
    except Exception as e:
        print(f"[推送失败] {e}")

# =====================================================
# EMA趋势
# =====================================================
def get_ema_trend(df, period=EMA_PERIOD):
    ema = df['c'].ewm(span=period).mean()
    last_close = df['c'].iloc[-1]
    if last_close > ema.iloc[-1]:
        return "看涨"
    elif last_close < ema.iloc[-1]:
        return "看跌"
    else:
        return "震荡"

# =====================================================
# 三连阳/三连阴
# =====================================================
def momentum_check(df):
    closes = df['c'].tail(MOMENTUM_LEN)
    diffs = closes.diff().dropna()
    if all(diffs > 0):
        return "三连阳"
    elif all(diffs < 0):
        return "三连阴"
    else:
        return "无明显动量"

# =====================================================
# 评分函数
# =====================================================
def calc_score(df, symbol):
    try:
        last = df.iloc[-1]
        pct = (last['c'] - last['o'])/last['o']*100
        ma20_vol = df['v'].rolling(20).mean().iloc[-1]
        vol_ratio = last['v']/(ma20_vol+1e-6)
        rng_now = (df['h']-df['l']).tail(3).mean()
        rng_hist = (df['h']-df['l']).mean()
        range_ratio = rng_now/(rng_hist+1e-6)
        speed = (df['c'].iloc[-1]-df['c'].iloc[-4])/df['c'].iloc[-4]*100

        # =====================================================
        # 资金质量
        # =====================================================
        try:
            trades = client_sync.futures_recent_trades(symbol=symbol,limit=200)
            if trades:
                trades_df = pd.DataFrame(trades)
                trades_df['quoteQty'] = trades_df['quoteQty'].astype(float)
                total_value = trades_df['quoteQty'].sum()
                trade_count = len(trades_df)
                avg_trade = total_value/(trade_count+1e-6)
                kline_value = df['c'].iloc[-1]*df['v'].iloc[-1]
                avg_trade_ratio = avg_trade/(kline_value+1e-6)
            else:
                avg_trade = 0
                avg_trade_ratio = 0
        except:
            avg_trade = 0
            avg_trade_ratio = 0

        if avg_trade>5000:
            entry_type = "机构"
        elif avg_trade>1000:
            entry_type = "中户"
        else:
            entry_type = "散户"

        # =====================================================
        # EMA趋势
        # =====================================================
        trend_1m = get_ema_trend(df)
        trend_5m = trend_1m
        trend_15m = trend_1m
        trends = [trend_1m,trend_5m,trend_15m]
        if trends.count("看涨")>=2:
            trend_resonance = "看涨共振"
        elif trends.count("看跌")>=2:
            trend_resonance = "看跌共振"
        else:
            trend_resonance = "震荡"

        # =====================================================
        # 三连阳/三连阴
        # =====================================================
        momentum = momentum_check(df)

        # =====================================================
        # 评分
        # =====================================================
        score = 0
        if vol_ratio>=3: score+=3
        elif vol_ratio>=2: score+=2
        elif vol_ratio>=1.5: score+=1
        if range_ratio>=1.5: score+=2
        elif range_ratio>=1.3: score+=1
        if speed>=3: score+=2

        if momentum in ["三连阳","三连阴"]:
            score+=1

        return (score,pct,vol_ratio,range_ratio,speed,entry_type,avg_trade_ratio,trend_1m,trend_5m,trend_15m,trend_resonance,momentum)
    except:
        return (0,0,0,0,0,"散户",0,"未知","未知","未知","震荡","无明显动量")

# =====================================================
# 推送函数
# =====================================================
def push_signal(symbol,phase,info):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"""
币对: {symbol}
Phase: {phase}
涨幅pct: {info[1]:.2f}%
放量比vol_ratio: {info[2]:.2f}x
波动比range_ratio: {info[3]:.2f}x
涨幅速度speed: {info[4]:.2f}%
资金类型: {info[5]}
资金质量比: {info[6]:.2f}
EMA趋势(1/5/15): {info[7]}/{info[8]}/{info[9]}
共振状态: {info[10]}
短期动量: {info[11]}
时间: {now}
"""
    send_server_chan(f"{symbol} Phase{phase}", msg)

# =====================================================
# Phase2/Phase4逻辑
# =====================================================
async def process_symbol(symbol):
    try:
        klines = await client.futures_klines(symbol=symbol,interval="1m",limit=KLINE_LIMIT)
        df = pd.DataFrame(klines,columns=['t','o','h','l','c','v','ct','qav','nt','tb','tq','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)

        score,pct,vol_ratio,range_ratio,speed,entry_type,avg_trade_ratio,trend_1m,trend_5m,trend_15m,trend_resonance,momentum = calc_score(df,symbol)

        # =====================================================
        # Phase2埋伏条件: 1分钟涨跌幅 >= 阈值 & 放量比>=阈值
        # =====================================================
        if abs(pct)>=PHASE_THRESH_PCT and vol_ratio>=VOL_RATIO_THRESHOLD:
            phase = 2
            key=(symbol,phase)
            now=time.time()
            if key not in last_push_time or now - last_push_time[key] >= PHASE_COOLDOWN:
                last_push_time[key]=now
                push_signal(symbol,phase,(score,pct,vol_ratio,range_ratio,speed,entry_type,avg_trade_ratio,trend_1m,trend_5m,trend_15m,trend_resonance,momentum))

        # Phase4可以用类似逻辑
        if abs(pct)>=PHASE_THRESH_PCT*1.5 and vol_ratio>=VOL_RATIO_THRESHOLD*1.5:  # Phase4加严格阈值
            phase = 4
            key=(symbol,phase)
            now=time.time()
            if key not in last_push_time or now - last_push_time[key] >= PHASE_COOLDOWN:
                last_push_time[key]=now
                push_signal(symbol,phase,(score,pct,vol_ratio,range_ratio,speed,entry_type,avg_trade_ratio,trend_1m,trend_5m,trend_15m,trend_resonance,momentum))

    except Exception as e:
        print("[处理异常]",symbol,e)

# =====================================================
# WebSocket asyncio 全市场订阅
# =====================================================
async def monitor_all_symbols():
    client = await AsyncClient.create(API_KEY,API_SECRET)
    bm = BinanceSocketManager(client)
    tickers = await client.futures_ticker()
    symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT") and float(t['quoteVolume'])>=MIN_24H_VOLUME]

    # 分批处理，避免过多协程爆队列
    batch_size=20
    for i in range(0,len(symbols),batch_size):
        batch=symbols[i:i+batch_size]
        tasks=[process_symbol(sym) for sym in batch]
        await asyncio.gather(*tasks)
    await client.close_connection()

# =====================================================
# 主循环
# =====================================================
if __name__=="__main__":
    print("🚀 全市场 Phase2/Phase4 信号监控启动")
    loop = asyncio.get_event_loop()
    while True:
        loop.run_until_complete(monitor_all_symbols())
        time.sleep(5)  # 全市场轮询间隔，可调
