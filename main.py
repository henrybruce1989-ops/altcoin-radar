# =====================================================
# 秒级爆发雷达 PRO（Tick + 结构 + 趋势 + 冷却）
# =====================================================

import time
from datetime import datetime, timedelta
from collections import deque, defaultdict
import requests
from binance import ThreadedWebsocketManager
from binance.client import Client
import pandas as pd
import os
import threading

# =====================================================
# ✅ 参数区（⭐核心调这里）
# =====================================================

MAX_SYMBOLS = 120
MIN_24H_USDT = 8_000_000

WINDOW = 15
THRESH = 0.2
VOL_MULT = 1.2

PHASE_ALERT = 2
PHASE_TRIGGER = 3

COOLDOWN = 300

PUSH_KEY = "sctp14659thuntd89pzhhlsmbwynooxu"

# =====================================================
# 初始化
# =====================================================

client = Client()

tick_cache = defaultdict(lambda: deque())
vol_cache = defaultdict(lambda: deque())

phase_map = {}
cooldown_map = {}

lock = threading.Lock()

# =====================================================
# 工具
# =====================================================

def now():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

def push(title, content):
    try:
        requests.post(f"https://sctapi.ftqq.com/{PUSH_KEY}.send",
                      data={"title": title, "desp": content}, timeout=5)
    except:
        pass

def save_csv(row):
    df = pd.DataFrame([row])
    df.to_csv("signals_15sec.csv", mode='a',
              header=not os.path.exists("signals.csv"),
              index=False, encoding='utf-8-sig')

# =====================================================
# ✅ 1分钟结构分析（压缩 + 吸筹 + 速度）
# =====================================================

def structure_analysis(symbol):
    try:
        kl = client.futures_klines(symbol=symbol, interval='1m', limit=20)

        df = pd.DataFrame(kl, columns=[
            't','o','h','l','c','v','ct','q','n','tb','tq','ig'
        ])

        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)

        # ===== 压缩 =====
        ranges = (df['h'] - df['l'])
        compress = ranges.tail(5).mean() < ranges.mean() * 0.7

        # ===== 吸筹 =====
        vol = df['v']
        accumulation = vol.tail(3).mean() > vol.mean()

        # ===== 速度 =====
        velocity = (df['c'].iloc[-1] - df['o'].iloc[-4]) / df['o'].iloc[-4] * 100

        # ===== 打标签 =====
        score = 0

        if compress:
            score += 2
            compress_label = "强"
        else:
            compress_label = "弱"

        if accumulation:
            score += 2
            acc_label = "强"
        else:
            acc_label = "弱"

        if velocity >= 3:
            score += 2
            vel_label = "强"
        elif velocity >= 1.5:
            score += 1
            vel_label = "中"
        else:
            vel_label = "弱"

        return score, compress_label, acc_label, vel_label, velocity

    except:
        return 0, "弱", "弱", "弱", 0

# =====================================================
# 趋势判断
# =====================================================

def get_trend(symbol, interval):
    try:
        kl = client.futures_klines(symbol=symbol, interval=interval, limit=200)
        df = pd.DataFrame(kl, columns=[
            't','o','h','l','c','v','ct','q','n','tb','tq','ig'
        ])
        df['close'] = df['c'].astype(float)
        df['ema144'] = df['close'].ewm(span=144).mean()

        above = (df['close'] > df['ema144']).sum()
        below = (df['close'] < df['ema144']).sum()

        if above / len(df) > 0.6:
            return "看涨"
        elif below / len(df) > 0.6:
            return "看跌"
        else:
            return "震荡"
    except:
        return "未知"

def multi_trend(symbol):
    t1 = get_trend(symbol, '1m')
    t5 = get_trend(symbol, '5m')
    t15 = get_trend(symbol, '15m')

    if t1 == t5 == t15:
        res = "共振"
    else:
        res = "无共振"

    return t1, t5, t15, res

# =====================================================
# 核心Tick逻辑
# =====================================================

def handle_trade(msg):
    try:
        if 'data' not in msg:
            return

        data = msg['data']
        if data['e'] != 'aggTrade':
            return

        symbol = data['s']
        price = float(data['p'])
        qty = float(data['q'])
        ts = time.time()

        tick_cache[symbol].append((ts, price))
        vol_cache[symbol].append((ts, price * qty))

        while tick_cache[symbol] and ts - tick_cache[symbol][0][0] > WINDOW:
            tick_cache[symbol].popleft()

        while vol_cache[symbol] and ts - vol_cache[symbol][0][0] > WINDOW:
            vol_cache[symbol].popleft()

        if len(tick_cache[symbol]) < 5:
            return

        start_price = tick_cache[symbol][0][1]
        pct = (price - start_price) / start_price * 100

        vol = sum(v for _, v in vol_cache[symbol])
        vol_ratio = vol / (len(vol_cache[symbol]) + 1e-6)

        with lock:
            phase = phase_map.get(symbol, 0)

            if abs(pct) >= THRESH and vol_ratio >= VOL_MULT:
                phase += 1
            else:
                phase = max(0, phase - 1)

            phase_map[symbol] = phase

        current_time = now()

        # =============================
        # Phase2：埋伏
        # =============================
        if phase == PHASE_ALERT:

            key = (symbol, "alert")
            if time.time() - cooldown_map.get(key, 0) < COOLDOWN:
                return

            cooldown_map[key] = time.time()

            push(f"{symbol} ⚠️埋伏",
                 f"{symbol}\n涨幅:{pct:.2f}%\n量:{vol_ratio:.2f}\n时间:{current_time}")

        # =============================
        # Phase4：开仓（结构过滤）
        # =============================
        if phase >= PHASE_TRIGGER:

            key = (symbol, "entry")
            if time.time() - cooldown_map.get(key, 0) < COOLDOWN:
                return

            # ⭐结构判断（核心）
            s_score, comp, acc, vel_label, velocity = structure_analysis(symbol)

            if s_score < 3:
                return

            cooldown_map[key] = time.time()

            direction = "🚀看涨" if pct > 0 else "🔻看跌"

            t1, t5, t15, res = multi_trend(symbol)

            msg_text = f"""
币对: {symbol}
信号: {direction}

【Tick】
15秒涨幅: {pct:.2f}%
成交量: {vol_ratio:.2f}

【结构】
压缩: {comp}
吸筹: {acc}
速度: {vel_label} ({velocity:.2f}%)

【趋势】
1m:{t1} 5m:{t5} 15m:{t15}
共振:{res}

时间: {current_time}
"""

            push(f"{symbol} 🚀开仓信号", msg_text)

            save_csv([
                current_time, symbol, direction,
                pct, vol_ratio,
                comp, acc, vel_label,
                t1, t5, t15, res
            ])

            phase_map[symbol] = 0

    except Exception as e:
        print("错误:", e)

# =====================================================
# 获取交易对
# =====================================================

def get_symbols():
    tickers = client.futures_ticker()
    symbols = [t['symbol'] for t in tickers
               if float(t['quoteVolume']) > MIN_24H_USDT]
    return symbols[:MAX_SYMBOLS]

# =====================================================
# 启动
# =====================================================

def start():
    symbols = get_symbols()

    streams = [f"{s.lower()}@aggTrade" for s in symbols]

    twm = ThreadedWebsocketManager()
    twm.start()

    twm.start_multiplex_socket(callback=handle_trade, streams=streams)

    while True:
        print(f"[{now()}] 运行中 | 监控:{len(symbols)}")
        time.sleep(10)

if __name__ == "__main__":
    print("🚀 PRO版本启动（结构+趋势）")
    start()
