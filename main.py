# =====================================================
# PRO版：三周期共振 秒级观察池雷达 + Phase推送 + EMA144趋势 + 资金质量
# =====================================================
import os
import csv
import time
import threading
from datetime import datetime
import pandas as pd
from binance.client import Client
# 🔧修改：删除旧WebSocket（避免爆队列）
# from binance import ThreadedWebsocketManager
import requests

# =====================================================
# ========== 可调参数区域 ==========
# =====================================================
API_KEY = os.getenv("API_KEY", "YOUR_BINANCE_API_KEY")
API_SECRET = os.getenv("API_SECRET", "YOUR_BINANCE_API_SECRET")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "sctp14659thuntd89pzhhlsmbwynooxu")

MIN_24H_VOLUME = 8_000_000
VOL_RATIO_THRESHOLD = 1.3
RANGE_RATIO_THRESHOLD = 1.3
TREND_COUNT_THRESHOLD = 3
PHASE_COOLDOWN = 300
OBSERVATION_TOPN = 80
SCAN_INTERVAL = 15
PHASE_MONITOR_INTERVAL = 5
SIGNAL_CSV = "signals_Promax.csv"
EMA_PERIOD = 144
KLINE_LIMIT = 100
PHASE_THRESH = 0.2

# ⭐新增：15秒预测参数
PREDICT_PCT = 0.15
PREDICT_VOL = 1.3
PREDICT_RANGE = 1.2
PREDICT_TREND = 2

# =====================================================
# 初始化
# =====================================================
client = Client(API_KEY, API_SECRET)

observation_pool = {}
lock = threading.Lock()

# =====================================================
# ⭐新增：15秒预测函数
# =====================================================
def predict_15s(symbol):
    try:
        klines = client.futures_klines(symbol=symbol, interval='15s', limit=25)
        df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qav','nt','tb','tq','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)

        vol_now = df['v'].tail(3).mean()
        vol_hist = df['v'].mean()
        vol_ratio = vol_now / (vol_hist + 1e-6)

        rng_now = (df['h'] - df['l']).tail(3).mean()
        rng_hist = (df['h'] - df['l']).mean()
        range_ratio = rng_now / (rng_hist + 1e-6)

        pct = (df['c'].iloc[-1] - df['o'].iloc[-1]) / df['o'].iloc[-1] * 100

        up = (df['c'].diff() > 0).tail(3).sum()
        down = (df['c'].diff() < 0).tail(3).sum()

        if pct > PREDICT_PCT and vol_ratio > PREDICT_VOL and range_ratio > PREDICT_RANGE and up >= PREDICT_TREND:
            return "↑上涨", pct
        elif pct < -PREDICT_PCT and vol_ratio > PREDICT_VOL and range_ratio > PREDICT_RANGE and down >= PREDICT_TREND:
            return "↓下跌", pct
        else:
            return "→中性", pct
    except Exception as e:
        print("[15秒预测异常]", e)
        return "未知", 0

# =====================================================
# Server酱推送（保留）
# =====================================================
def send_server_chan(title, content):
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    data = {"title": title, "desp": content}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"[推送失败] {e}")

# =====================================================
# CSV保存（🔧新增字段）
# =====================================================
def save_csv(data):
    file_exists = os.path.isfile(SIGNAL_CSV)
    with open(SIGNAL_CSV,"a",newline='',encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "time","symbol","phase","score","pct","vol_ratio","range_ratio",
                "trend_count","speed","compression","accumulation",
                "avg_trade","avg_trade_ratio","entry_type",
                "trend_ema144_1m","trend_ema144_5m","trend_ema144_15m","trend_resonance",
                "predict_dir","predict_pct"  # ⭐新增
            ])
        writer.writerow(data)

# =====================================================
# EMA趋势（保留）
# =====================================================
def get_ema_trend(symbol, interval):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=KLINE_LIMIT)
        df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qav','nt','tb','tq','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        ema = df['c'].ewm(span=EMA_PERIOD).mean()
        last_close = df['c'].iloc[-1]
        if last_close > ema.iloc[-1]:
            return "看涨"
        elif last_close < ema.iloc[-1]:
            return "看跌"
        else:
            return "震荡"
    except:
        return "未知"

def calc_score(df, symbol):
    try:
        last = df.iloc[-1]
        pct = (last['c'] - last['o']) / last['o'] * 100

        ma20_vol = df['v'].rolling(20).mean().iloc[-1]
        vol_ratio = last['v'] / (ma20_vol + 1e-6)

        rng_now = (df['h'] - df['l']).tail(3).mean()
        rng_hist = (df['h'] - df['l']).mean()
        range_ratio = rng_now / (rng_hist + 1e-6)

        trend_count = max(
            (df['c'].diff() > 0).tail(5).sum(),
            (df['c'].diff() < 0).tail(5).sum()
        )

        speed = (df['c'].iloc[-1] - df['c'].iloc[-4]) / df['c'].iloc[-4] * 100

        compression = "强" if (df['h'] - df['l']).tail(5).mean() < 0.5*(df['h'] - df['l']).mean() else "弱"
        accumulation = "强" if df['v'].tail(5).mean() < 0.5*df['v'].mean() else "弱"

        # =====================================================
        # ⭐资金质量（独立 try，不影响主流程）
        # =====================================================
        try:
            trades = client.futures_recent_trades(symbol=symbol, limit=200)

            if trades:
                trades_df = pd.DataFrame(trades)

                trades_df['q'] = trades_df['q'].astype(float)
                trades_df['p'] = trades_df['p'].astype(float)
                trades_df['value'] = trades_df['q'] * trades_df['p']

                total_value = trades_df['value'].sum()
                trade_count = len(trades_df)

                avg_trade = total_value / (trade_count + 1e-6)

                kline_value = df['c'].iloc[-1] * df['v'].iloc[-1]
                avg_trade_ratio = avg_trade / (kline_value + 1e-6)
            else:
                avg_trade = 0
                avg_trade_ratio = 0

        except Exception as e:
            print("[资金质量异常]", symbol, e)
            avg_trade = 0
            avg_trade_ratio = 0

        # =====================================================
        # ⭐资金分类（必须在 try 外）
        # =====================================================
        if avg_trade > 5000:
            entry_type = "机构"
        elif avg_trade > 1000:
            entry_type = "中户"
        else:
            entry_type = "散户"

        # =====================================================
        # EMA趋势
        # =====================================================
        trend_1m = get_ema_trend(symbol, "1m")
        trend_5m = get_ema_trend(symbol, "5m")
        trend_15m = get_ema_trend(symbol, "15m")

        trends = [trend_1m, trend_5m, trend_15m]

        if trends.count("看涨") >= 2:
            trend_resonance = "看涨共振"
        elif trends.count("看跌") >= 2:
            trend_resonance = "看跌共振"
        else:
            trend_resonance = "震荡"

        # =====================================================
        # ⭐15秒预测
        # =====================================================
        predict_dir, predict_pct = predict_15s(symbol)

        # =====================================================
        # ⭐评分
        # =====================================================
        score = 0

        if vol_ratio >= 3:
            score += 3
        elif vol_ratio >= 2:
            score += 2
        elif vol_ratio >= 1.5:
            score += 1

        if range_ratio >= 1.5:
            score += 2
        elif range_ratio >= 1.3:
            score += 1

        if trend_count >= 4:
            score += 2

        if speed >= 3:
            score += 2

        if compression == "强" and accumulation == "强":
            score += 1

        return (
            score, pct, vol_ratio, range_ratio, trend_count, speed,
            compression, accumulation,
            avg_trade, avg_trade_ratio, entry_type,
            trend_1m, trend_5m, trend_15m, trend_resonance,
            predict_dir, predict_pct
        )

    except Exception as e:
        print("[评分异常]", symbol, e)
        return (
            0,0,0,0,0,0,
            "弱","弱",
            0,0,"散户",
            "未知","未知","未知","震荡",
            "未知",0
        )
# =====================================================
# 🔧修改：加入“进入观察池逻辑”
# =====================================================
def update_observation_pool():
    try:
        tickers = client.futures_ticker()
        symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT") and float(t['quoteVolume'])>=MIN_24H_VOLUME]

        scored_list = []

        for s in symbols:
            klines = client.futures_klines(symbol=s, interval='1m', limit=KLINE_LIMIT)
            df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qav','nt','tb','tq','ig'])
            df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)

            # ⭐进入条件
            vol_now = df['v'].tail(3).mean()
            vol_hist = df['v'].mean()
            cond_vol = vol_now > vol_hist * VOL_RATIO_THRESHOLD

            rng_now = (df['h'] - df['l']).tail(3).mean()
            rng_hist = (df['h'] - df['l']).mean()
            cond_range = rng_now > rng_hist * RANGE_RATIO_THRESHOLD

            trend = (df['c'].diff() > 0).tail(5).sum()
            cond_trend = trend >= TREND_COUNT_THRESHOLD or trend <= 1

            if not (cond_vol or cond_range or cond_trend):
                continue  # ❗关键过滤（防爆）

            result = calc_score(df, s)

            scored_list.append({'symbol':s,'score':result[0],'df':df,
                                'pct':result[1],'vol_ratio':result[2],'range_ratio':result[3],
                                'trend_count':result[4],'speed':result[5],
                                'compression':result[6],'accumulation':result[7],
                                'avg_trade':result[8],'avg_trade_ratio':result[9],
                                'entry_type':result[10],
                                'trend_1m':result[11],'trend_5m':result[12],'trend_15m':result[13],
                                'trend_resonance':result[14],
                                'predict_dir':result[15],'predict_pct':result[16]})

        scored_list.sort(key=lambda x:x['score'], reverse=True)
        top_symbols = scored_list[:OBSERVATION_TOPN]

        with lock:
            for item in top_symbols:
                s = item['symbol']
                if s not in observation_pool:
                    observation_pool[s] = {'last_signal_time': datetime.min, 'phase':1}
                observation_pool[s].update(item)

    except Exception as e:
        print("[观察池更新异常]", e)

# =====================================================
# 🔧修改：推送增加预测信息
# =====================================================
def push_phase_signal(symbol, info, phase):
    msg = f"""
币对: {symbol}
Phase: {phase}
评分: {info['score']}
涨幅: {info['pct']:.2f}%
放量比: {info['vol_ratio']:.2f}x
波动比: {info['range_ratio']:.2f}x
涨幅速度: {info['speed']:.2f}%

压缩: {info['compression']} 吸筹: {info['accumulation']}
资金类型: {info['entry_type']}
资金质量比: {info['avg_trade_ratio']:.2f}

趋势: 1m:{info['trend_1m']} 5m:{info['trend_5m']} 15m:{info['trend_15m']}
共振: {info['trend_resonance']}

🚀15秒预测: {info['predict_dir']} ({info['predict_pct']:.2f}%)

时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    send_server_chan(f"{symbol} Phase{phase}", msg)

# =====================================================
# 线程（保留）
# =====================================================
def observation_pool_thread():
    while True:
        update_observation_pool()
        time.sleep(SCAN_INTERVAL)

def phase_monitor_thread():
    while True:
        with lock:
            for s, info in observation_pool.items():
                if (datetime.now() - info['last_signal_time']).total_seconds() < PHASE_COOLDOWN:
                    continue
                if info['phase']==1 and info['pct']>PHASE_THRESH:
                    push_phase_signal(s, info, 2)
                    info['phase']=2
                    info['last_signal_time']=datetime.now()
                elif info['phase']==2 and info['pct']>PHASE_THRESH:
                    push_phase_signal(s, info, 4)
                    info['phase']=1
                    info['last_signal_time']=datetime.now()
        time.sleep(PHASE_MONITOR_INTERVAL)

# =====================================================
# 启动
# =====================================================
if __name__=="__main__":
    threading.Thread(target=observation_pool_thread, daemon=True).start()
    threading.Thread(target=phase_monitor_thread, daemon=True).start()

    print("🚀 PRO终极系统运行中（稳定版）")

    while True:
        time.sleep(1)
