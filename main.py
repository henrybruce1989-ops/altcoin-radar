import asyncio
import time
import aiohttp
import pandas as pd
from collections import deque, defaultdict
from datetime import datetime, timedelta
from binance import AsyncClient, BinanceSocketManager

# =====================================================
# 参数设置
# =====================================================
API_KEY = ""
API_SECRET = ""
SERVER_CHAN_KEY = "sctp14659thuntd89pzhhlsmbwynooxu"

MIN_24H_VOLUME = 8_000_000 
EMA_PERIOD = 144
TREND_WINDOW = 100
TREND_RATIO = 0.7 

# 策略阈值：只要达标就推送，不拦截
STRATEGY = {
    "1m": {"pct": 0.6, "vol": 1.5, "cooldown": 60},
    "3m": {"pct": 1.0, "vol": 2.0, "cooldown": 120}
}

# 数据容器
klines_1m = defaultdict(lambda: deque(maxlen=300))
klines_5m = defaultdict(lambda: deque(maxlen=300))
klines_15m = defaultdict(lambda: deque(maxlen=300))
# 用于计算速率的毫秒级价格缓存 (最近10秒)
price_speed_cache = defaultdict(lambda: deque(maxlen=10)) 

last_push_time = {}

# =====================================================
# 趋势共振分析 (仅作为参考信息输出)
# =====================================================
def analyze_resonance(symbol):
    stats = {}
    for interval, container in [("1m", klines_1m), ("5m", klines_5m), ("15m", klines_15m)]:
        data = list(container[symbol])
        if len(data) < 244: return "初始化中", "N/A"
        
        df = pd.DataFrame(data)
        ema = df['c'].ewm(span=EMA_PERIOD, adjust=False).mean()
        ratio = (df['c'].tail(TREND_WINDOW) > ema.tail(TREND_WINDOW)).sum() / TREND_WINDOW
        stats[interval] = ratio

    # 判断共振标签
    if all(v >= TREND_RATIO for v in stats.values()):
        tag = "🔥 三周期强共振 (多头)"
    elif all(v <= (1 - TREND_RATIO) for v in stats.values()):
        tag = "❄️ 三周期强共振 (空头)"
    else:
        tag = "⚠️ 无共振 (震荡/反弹)"
    
    detail = f"1m:{stats['1m']:.0%}/5m:{stats['5m']:.0%}/15m:{stats['15m']:.0%}"
    return tag, detail

# =====================================================
# 信号检测逻辑
# =====================================================
async def detect_signal(symbol, interval):
    data = list(klines_1m[symbol])
    if interval == "3m":
        if len(data) < 3: return
        subset = data[-3:]
        o_price, c_price = subset[0]['o'], subset[-1]['c']
        v_sum = sum(d['v'] for d in subset)
        vol_ref = pd.Series([d['v'] for d in data]).rolling(60).mean().iloc[-1] * 3
    else:
        last_k = data[-1]
        o_price, c_price, v_sum = last_k['o'], last_k['c'], last_k['v']
        vol_ref = pd.Series([d['v'] for d in data]).iloc[-21:-1].mean()

    actual_pct = (c_price - o_price) / o_price * 100
    actual_vol = v_sum / (vol_ref + 1e-9)
    
    conf = STRATEGY[interval]

    # 1. 异动硬性拦截：只看涨幅和放量
    if abs(actual_pct) >= conf['pct'] and actual_vol >= conf['vol']:
        
        # 2. 冷却检查
        now = time.time()
        if now - last_push_time.get((symbol, interval), 0) < conf['cooldown']: return
        
        # 3. 计算涨幅速率 (Price Velocity)
        # 获取10秒前的价格，计算10秒内的瞬间变动
        speed_data = price_speed_cache[symbol]
        velocity = 0
        if len(speed_data) >= 2:
            velocity = (c_price - speed_data[0]) / speed_data[0] * 100

        # 4. 获取趋势共振参考 (不做拦截)
        res_tag, res_detail = analyze_resonance(symbol)
        
        last_push_time[(symbol, interval)] = now
        bj_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%H:%M:%S')

        title = f"[{interval}异动] {symbol}"
        msg = (f"⏰ 时间: {bj_time} (北京)\n"
               f"💰 价格: {c_price}\n"
               f"📈 周期涨幅: {actual_pct:.2f}% (标:{conf['pct']}%)\n"
               f"🚀 瞬间速率: {velocity:.3f}% (10s内)\n"
               f"📊 放量倍数: {actual_vol:.2f}x (标:{conf['vol']}x)\n"
               f"🧬 趋势共振: {res_tag}\n"
               f"📜 详情: {res_detail}")
        
        asyncio.create_task(send_push(title, msg))

async def send_push(title, msg):
    if not SERVER_CHAN_KEY: return
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send", 
                               data={"title": title, "desp": msg}, timeout=5)
        except: pass

# =====================================================
# WS 数据引擎
# =====================================================
async def run_market_radar(client, symbols):
    bm = BinanceSocketManager(client)
    # 同时监听 K线 和 实时价格(用于算速率)
    streams = []
    for s in symbols:
        streams.extend([f"{s.lower()}@kline_1m", f"{s.lower()}@kline_5m", f"{s.lower()}@kline_15m", f"{s.lower()}@ticker"])
    
    async with bm.multiplex_socket(streams) as stream:
        while True:
            try:
                res = await stream.recv()
                data = res['data']
                s = data['s']
                
                # 处理 Ticker (每秒推一次，用于算速率)
                if res['stream'].endswith('@ticker'):
                    price_speed_cache[s].append(float(data['c']))
                    continue

                # 处理 K线
                k = data['k']
                if not k['x']: continue # 只处理收盘

                p_load = {'o':float(k['o']),'h':float(k['h']),'l':float(k['l']),'c':float(k['c']),'v':float(k['v'])}
                
                if k['i'] == '1m':
                    klines_1m[s].append(p_load)
                    await detect_signal(s, "1m")
                    if int(datetime.now().minute) % 3 == 0:
                        await detect_signal(s, "3m")
                elif k['i'] == '5m':
                    klines_5m[s].append(p_load)
                elif k['i'] == '15m':
                    klines_15m[s].append(p_load)
            except: await asyncio.sleep(1)

async def preload(client, symbols):
    print("⏳ 预加载历史数据并同步指标...")
    for s in symbols:
        for itv, container in [('1m', klines_1m), ('5m', klines_5m), ('15m', klines_15m)]:
            kls = await client.futures_klines(symbol=s, interval=itv, limit=300)
            for k in kls:
                container[s].append({'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5])})
    print("✅ 系统就绪")

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        tickers = await client.futures_ticker()
        symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT") and float(t['quoteVolume']) > MIN_24H_VOLUME]
        await preload(client, symbols)
        shard_size = 15
        await asyncio.gather(*[run_market_radar(client, symbols[i:i+shard_size]) for i in range(0, len(symbols), shard_size)])
    finally: await client.close_connection()

if __name__ == "__main__":
    asyncio.run(main())
