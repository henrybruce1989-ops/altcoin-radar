# =====================================================
# 🚀 V11 WebSocket低延迟交易系统（参数可调版）
# =====================================================

import os
import time
from datetime import datetime, timedelta, timezone
import threading
import requests
from collections import deque
from binance.client import Client
from binance import ThreadedWebsocketManager

# =====================================================
# 🧠【参数配置区】——你以后主要调这里
# =====================================================

# ===== Micro（秒级信号） =====
MICRO_WINDOW_SECONDS = 15      # 统计多少秒的成交数据（建议 10~30）
MICRO_PCT_THRESHOLD = 0.3     # 触发涨跌幅（%）👉 核心参数
MICRO_MIN_TRADES = 5          # 最少成交笔数（过滤噪音）

# ===== K线确认 =====
KLINE_1M_THRESHOLD = 1.0      # 1m涨幅确认
KLINE_3M_THRESHOLD = 3.0      # 3m趋势确认

# ===== 止盈止损 =====
STOP_LOSS_PCT = -1.0          # 止损（%）
TAKE_PROFIT_PCT = 2.0         # 止盈（%）

# ===== 推送控制 =====
MIN_SCORE_TO_ALERT = 2        # 最低推送强度（避免刷屏）

# =====================================================
# API配置
# =====================================================

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
SERVER_KEY = os.getenv("sctp14659thuntd89pzhhlsmbwynooxu")

client = Client(API_KEY, API_SECRET)

BEIJING_TZ = timezone(timedelta(hours=8))

def now():
    return datetime.now(BEIJING_TZ)

# =====================================================
# 📩 推送模块（Server酱）
# =====================================================

def push(msg):
    try:
        requests.post(
            f"https://sctapi.ftqq.com/{SERVER_KEY}.send",
            data={"title": "交易信号", "desp": msg},
            timeout=5
        )
    except:
        pass

# =====================================================
# 📦 数据缓存（核心：全部本地维护）
# =====================================================

trade_cache = {}   # 秒级成交缓存
kline_1m = {}      # 1分钟K线
kline_3m = {}      # 3分钟K线

positions = {}     # 当前持仓状态

# =====================================================
# 🚀 Micro检测（秒级核心）
# =====================================================

def detect_micro(symbol):

    trades = trade_cache.get(symbol)

    # ===== 数据不足直接跳过 =====
    if not trades or len(trades) < MICRO_MIN_TRADES:
        return None

    prices = [t[0] for t in trades]
    qtys = [t[1] for t in trades]

    # ===== 计算涨跌幅 =====
    pct = (prices[-1] - prices[0]) / prices[0] * 100

    volume = sum(qtys)

    # ===== 核心过滤条件（可调）=====
    if abs(pct) < MICRO_PCT_THRESHOLD:
        return None

    return pct, volume

# =====================================================
# 📊 K线评分（趋势确认）
# =====================================================

def kline_score(data, threshold):

    if not data or len(data) < 2:
        return None

    o, c, v = data[-1]

    pct = (c - o) / o * 100

    # ===== 是否满足趋势强度 =====
    if abs(pct) < threshold:
        return None

    return pct

# =====================================================
# 🧠 主交易逻辑（核心引擎）
# =====================================================

def process_signal(symbol):

    micro = detect_micro(symbol)

    k1 = kline_score(kline_1m.get(symbol), KLINE_1M_THRESHOLD)
    k3 = kline_score(kline_3m.get(symbol), KLINE_3M_THRESHOLD)

    # ===== 没有micro直接退出（先手必须）=====
    if not micro:
        return

    pct_micro, vol = micro

    direction = "LONG" if pct_micro > 0 else "SHORT"

    pos = positions.get(symbol)

    price = trade_cache[symbol][-1][0]

    # =====================================================
    # 🟢 开仓（第一阶段）
    # =====================================================
    if not pos:

        positions[symbol] = {
            "stage": 1,
            "entry": price,
            "direction": direction
        }

        push(f"{symbol} 🟡Micro开仓\n涨幅:{pct_micro:.2f}%")

        return

    # =====================================================
    # 🟠 加仓（1m确认）
    # =====================================================
    if pos["stage"] == 1 and k1:

        pos["stage"] = 2

        push(f"{symbol} 🟠加仓（1m确认）\n1m涨幅:{k1:.2f}%")

    # =====================================================
    # 🔴 满仓（3m趋势）
    # =====================================================
    elif pos["stage"] == 2 and k3:

        pos["stage"] = 3

        push(f"{symbol} 🔴满仓（3m趋势）\n3m涨幅:{k3:.2f}%")

    # =====================================================
    # 💰 止盈止损
    # =====================================================

    pnl = (price - pos["entry"]) / pos["entry"] * 100

    if direction == "SHORT":
        pnl = -pnl

    # ===== 止损 =====
    if pnl < STOP_LOSS_PCT:

        push(f"{symbol} ❌止损 {pnl:.2f}%")
        positions.pop(symbol)
        return

    # ===== 止盈 =====
    if pnl > TAKE_PROFIT_PCT:

        push(f"{symbol} ✅止盈 {pnl:.2f}%")
        positions.pop(symbol)
        return

# =====================================================
# 📡 WebSocket：成交流（最重要）
# =====================================================

def handle_trade(msg):

    symbol = msg['s']
    price = float(msg['p'])
    qty = float(msg['q'])
    t = time.time()

    if symbol not in trade_cache:
        trade_cache[symbol] = deque()

    trade_cache[symbol].append((price, qty, t))

    # ===== 滑动窗口（秒级）=====
    while trade_cache[symbol] and t - trade_cache[symbol][0][2] > MICRO_WINDOW_SECONDS:
        trade_cache[symbol].popleft()

    process_signal(symbol)

# =====================================================
# 📊 K线（1m）
# =====================================================

def handle_kline_1m(msg):

    k = msg['k']
    symbol = msg['s']

    if not k['x']:
        return

    o = float(k['o'])
    c = float(k['c'])
    v = float(k['v'])

    kline_1m.setdefault(symbol, []).append((o,c,v))

# =====================================================
# 📊 K线（3m）
# =====================================================

def handle_kline_3m(msg):

    k = msg['k']
    symbol = msg['s']

    if not k['x']:
        return

    o = float(k['o'])
    c = float(k['c'])
    v = float(k['v'])

    kline_3m.setdefault(symbol, []).append((o,c,v))

# =====================================================
# 🚀 主程序
# =====================================================

def main():

    symbols = [
        s["symbol"]
        for s in client.futures_ticker()
        if s["symbol"].endswith("USDT")
        and float(s["quoteVolume"]) > 6000000
    ]

    print("交易对数量:", len(symbols))

    twm = ThreadedWebsocketManager(
        api_key=API_KEY,
        api_secret=API_SECRET
    )

    twm.start()

    for s in symbols:
        twm.start_aggtrade_socket(callback=handle_trade, symbol=s)
        twm.start_kline_socket(callback=handle_kline_1m, symbol=s, interval="1m")
        twm.start_kline_socket(callback=handle_kline_3m, symbol=s, interval="3m")

    while True:
        print(f"[{now().strftime('%H:%M:%S')}] 运行中 | 持仓:{len(positions)}")
        time.sleep(10)

# =====================================================

if __name__ == "__main__":
    main()
