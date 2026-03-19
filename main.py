# =====================================================
# 全市场 Phase2 WebSocket 实时监控（完整版）
# =====================================================
import os, csv, time, threading
from datetime import datetime, timezone, timedelta
from collections import deque
import pandas as pd
import requests
from binance.um_futures import UMFutures

# =====================================================
# 配置参数
# =====================================================
API_KEY = os.getenv("API_KEY", "YOUR_BINANCE_API_KEY")
API_SECRET = os.getenv("API_SECRET", "YOUR_BINANCE_API_SECRET")
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "sctp14659thuntd89pzhhlsmbwynooxu")

PHASE_COOLDOWN = 300                # Phase冷却时间，秒
WINDOW_SECONDS = 15                 # 15秒聚合窗口
MAX_QUEUE_LEN = 1000                # 每交易对队列上限
EMA_PERIOD = 144                    # EMA周期
PHASE_THRESH = 0.2                  # 15秒窗口涨幅阈值
PREDICT_VOL_RATIO = 1.3             # 放量阈值
PREDICT_MQ_RATIO = 1.2              # 资金质量比阈值
SIGNAL_CSV = "signals_Promax_WS.csv"
MIN_24H_VOLUME = 8_000_000          # 全市场最低24h成交额筛选
OBSERVATION_TOPN = 200              # 可选TopN，保证订阅量可控

# =====================================================
# 初始化客户端
# =====================================================
client = UMFutures(key=API_KEY, secret=API_SECRET)
trade_queues = {}                   # symbol -> deque
observation_pool = {}               # symbol -> phase状态 + 上次推送
lock = threading.Lock()

# =====================================================
# Server酱推送
# =====================================================
def send_server_chan(title, content):
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    try:
        requests.post(url, data={"title": title, "desp": content}, timeout=5)
    except Exception as e:
        print("[Server酱推送失败]", e)

# =====================================================
# CSV保存
# =====================================================
def save_csv(data):
    file_exists = os.path.isfile(SIGNAL_CSV)
    with open(SIGNAL_CSV, "a", newline='', encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "time","symbol","phase","pct","vol_ratio","range_ratio","speed",
                "compression","accumulation","mq_ratio",
                "trend_1m","trend_5m","trend_15m","trend_resonance",
                "predict_dir"
            ])
        writer.writerow(data)

# =====================================================
# EMA趋势与共振
# =====================================================
def get_ema_trend(symbol, interval):
    try:
        klines = client.klines(symbol=symbol, interval=interval, limit=100)
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

def calc_trend_resonance(trend1, trend5, trend15):
    trends = [trend1, trend5, trend15]
    if trends.count("看涨") >= 2:
        return "看涨共振"
    elif trends.count("看跌") >= 2:
        return "看跌共振"
    else:
        return "震荡"

# =====================================================
# WebSocket接收每笔成交
# =====================================================
def ws_listen(symbol):
    global trade_queues
    while True:
        try:
            print(f"[WS启动] {symbol}")
            stream = client.aggTrade_socket(symbol.lower())
            for msg in stream:
                price = float(msg['p'])
                qty = float(msg['q'])
                quote_qty = price * qty
                ts = int(time.time()*1000)
                with lock:
                    if symbol not in trade_queues:
                        trade_queues[symbol] = deque(maxlen=MAX_QUEUE_LEN)
                    trade_queues[symbol].append({'price': price, 'qty': qty, 'quoteQty': quote_qty, 'time': ts})
        except Exception as e:
            print(f"[WS异常]{symbol} {e}, 3秒重连...")
            time.sleep(3)
            continue

# =====================================================
# Phase2监控（15秒窗口聚合 + 推送）
# =====================================================
def phase_monitor(symbol):
    global trade_queues, observation_pool
    while True:
        time.sleep(1)
        with lock:
            if symbol not in trade_queues or not trade_queues[symbol]:
                continue
            now_ts = int(time.time()*1000)
            window_start = now_ts - WINDOW_SECONDS*1000
            trades = [t for t in trade_queues[symbol] if t['time']>=window_start]
            if not trades:
                continue

            # 当前窗口开盘/收盘价
            o = trades[0]['price']
            c = trades[-1]['price']
            pct = (c - o) / o * 100

            # 当前窗口总量 & 历史平均量
            vol_now = sum([t['qty'] for t in trades])
            vol_hist = pd.Series([t['qty'] for t in trade_queues[symbol]]).mean() * len(trades)
            vol_ratio = vol_now / (vol_hist+1e-6)

            # ⭐ 新增：波动比 range_ratio
            rng_now = max(t['price'] for t in trades) - min(t['price'] for t in trades)
            rng_hist = max(t['price'] for t in trade_queues[symbol]) - min(t['price'] for t in trade_queues[symbol])
            range_ratio = rng_now / (rng_hist + 1e-6) if rng_hist>0 else 0

            # 资金质量比
            mq_ratio = pd.Series([t['quoteQty'] for t in trades]).mean() / (pd.Series([t['quoteQty'] for t in trade_queues[symbol]]).mean()+1e-6)

            # 涨幅速度
            speed = (trades[-1]['price'] - trades[0]['price']) / trades[0]['price'] * 100

            # 压缩 / 吸筹
            compression = "强" if (max(t['price'] for t in trades)-min(t['price'] for t in trades)) < 0.5*(max(t['price'] for t in trade_queues[symbol])-min(t['price'] for t in trade_queues[symbol])) else "弱"
            accumulation = "强" if vol_now < 0.5*vol_hist else "弱"

            # EMA趋势 & 共振
            trend_1m = get_ema_trend(symbol,'1m')
            trend_5m = get_ema_trend(symbol,'5m')
            trend_15m = get_ema_trend(symbol,'15m')
            trend_resonance = calc_trend_resonance(trend_1m,trend_5m,trend_15m)

            # Phase状态机
            if symbol not in observation_pool:
                observation_pool[symbol] = {'phase':1, 'last_signal_time':datetime.min}

            info = observation_pool[symbol]
            if (datetime.now() - info['last_signal_time']).total_seconds() < PHASE_COOLDOWN:
                continue

            # 15秒预测方向
            predict_dir = "→中性"
            if pct >= PHASE_THRESH:
                predict_dir = "↑上涨"
            elif pct <= -PHASE_THRESH:
                predict_dir = "↓下跌"

            # Phase2埋伏触发
            if info['phase']==1:
                if predict_dir in ["↑上涨","↓下跌"] and vol_ratio>=PREDICT_VOL_RATIO and mq_ratio>=PREDICT_MQ_RATIO:
                    push_phase(symbol, info, 2, pct, vol_ratio, range_ratio, mq_ratio, speed, compression, accumulation, trend_1m, trend_5m, trend_15m, trend_resonance, predict_dir)
            elif info['phase']==2:
                if predict_dir in ["↑上涨","↓下跌"] and vol_ratio>=PREDICT_VOL_RATIO and mq_ratio>=PREDICT_MQ_RATIO:
                    push_phase(symbol, info, 4, pct, vol_ratio, range_ratio, mq_ratio, speed, compression, accumulation, trend_1m, trend_5m, trend_15m, trend_resonance, predict_dir)

# =====================================================
# Phase推送函数（保留原有内容 + range_ratio）
# =====================================================
def push_phase(symbol, info, phase, pct, vol_ratio, range_ratio, mq_ratio, speed, compression, accumulation, trend_1m, trend_5m, trend_15m, trend_resonance, predict_dir):
    info['phase'] = 2 if phase==2 else 1
    info['last_signal_time'] = datetime.now()
    t_str = (datetime.now(timezone.utc)+timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    msg = f"""
币对: {symbol}
Phase: {phase} {"埋伏" if phase==2 else "开仓"}
涨幅pct: {pct:.2f}%
放量比vol_ratio: {vol_ratio:.2f}x
波动比range_ratio: {range_ratio:.2f}x
涨幅速度speed: {speed:.2f}%
压缩/吸筹: {compression} / {accumulation}
资金质量比mq_ratio: {mq_ratio:.2f}
EMA趋势: 1m:{trend_1m} 5m:{trend_5m} 15m:{trend_15m}
共振状态: {trend_resonance}
15秒预测方向: {predict_dir}
时间(GMT+8): {t_str}
"""
    print(msg)
    send_server_chan(f"{symbol} Phase{phase}", msg)
    save_csv([t_str,symbol,phase,pct,vol_ratio,range_ratio,speed,compression,accumulation,mq_ratio,trend_1m,trend_5m,trend_15m,trend_resonance,predict_dir])

# =====================================================
# 全市场启动函数
# =====================================================
def get_top_symbols():
    tickers = client.ticker_24hr()
    symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT") and float(t['quoteVolume'])>=MIN_24H_VOLUME]
    symbols.sort(reverse=True)  # 可选TopN
    return symbols[:OBSERVATION_TOPN]

if __name__=="__main__":
    symbols = get_top_symbols()
    for s in symbols:
        threading.Thread(target=ws_listen,args=(s,),daemon=True).start()
        threading.Thread(target=phase_monitor,args=(s,),daemon=True).start()
    print(f"🚀 全市场 Phase2 WebSocket监控启动，监控币种数: {len(symbols)}")
    while True:
        time.sleep(1)
