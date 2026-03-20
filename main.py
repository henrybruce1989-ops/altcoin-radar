import asyncio
import time
import aiohttp
import pandas as pd
from collections import deque, defaultdict
from datetime import datetime
from binance import AsyncClient, BinanceSocketManager

# =====================================================
# 严格参数区
# =====================================================
API_KEY = ""
API_SECRET = ""
SERVER_CHAN_KEY = "sctp14659thuntd89pzhhlsmbwynooxu"

MIN_24H_VOLUME = 10_000_000 # 提高门槛，过滤僵尸币
PHASE_THRESH_PCT = 0.6      # 1分钟涨幅必须 >= 0.6%
VOL_RATIO_THRESHOLD = 1.2   # 1分钟成交量必须 >= 20min均值的1.5倍
EMA_PERIOD = 144
TREND_WINDOW = 100          # 回溯100根K线
TREND_RATIO = 0.7           # 70%时间在均线上方才算看涨

# =====================================================
# 核心数据容器
# =====================================================
# 必须存储足够的K线来计算 EMA144 + 100根统计窗
kline_vault = defaultdict(lambda: deque(maxlen=300)) 
last_push_time = {}

# =====================================================
# 趋势与评分引擎（严格逻辑版）
# =====================================================
def analyze_market_standard(symbol):
    data = list(kline_vault[symbol])
    if len(data) < 244: return None # 144+100 根基础要求
    
    df = pd.DataFrame(data)
    df['c'] = df['c'].astype(float)
    df['v'] = df['v'].astype(float)
    df['o'] = df['o'].astype(float)

    # 1. 计算当前这根 K 线的真实表现 (收盘 vs 开盘)
    last_k = df.iloc[-1]
    actual_pct = (last_k['c'] - last_k['o']) / last_k['o'] * 100
    
    # 2. 计算真实放量比 (当前分钟量 / 过去20分钟均值)
    vol_ma20 = df['v'].iloc[-21:-1].mean() # 不含当前根，算前20根的均值
    actual_vol_ratio = last_k['v'] / (vol_ma20 + 1e-9)

    # 3. 严格趋势占比判断 (过去100根K线)
    ema144_series = df['c'].ewm(span=EMA_PERIOD, adjust=False).mean()
    # 统计过去100根K线收盘价 > EMA144 的数量
    window_c = df['c'].tail(TREND_WINDOW)
    window_ema = ema144_series.tail(TREND_WINDOW)
    above_count = (window_c > window_ema).sum()
    occupancy = above_count / TREND_WINDOW

    trend_label = "震荡"
    if occupancy >= TREND_RATIO: trend_label = "看涨"
    elif occupancy <= (1 - TREND_RATIO): trend_label = "看跌"

    return {
        "pct": actual_pct,
        "vol_ratio": actual_vol_ratio,
        "trend": trend_label,
        "ratio": occupancy,
        "price": last_k['c']
    }

# =====================================================
# 信号触发逻辑
# =====================================================
async def on_kline_closed(symbol):
    res = analyze_market_standard(symbol)
    if not res: return

    abs_pct = abs(res['pct'])
    vol_r = res['vol_ratio']

    # 严格阈值拦截：不达标绝对不推
    phase = 0
    if abs_pct >= PHASE_THRESH_PCT * 1.5: 
        phase = 4
    elif abs_pct >= PHASE_THRESH_PCT and vol_r >= VOL_RATIO_THRESHOLD:
        phase = 2
    
    if phase == 0: return

    # 冷却检查
    now = time.time()
    if now - last_push_time.get((symbol, phase), 0) < 120: # 提高冷却到2分钟
        return
    
    last_push_time[(symbol, phase)] = now

    msg = (f"🚨 {symbol} P{phase} 触发\n"
           f"分钟涨幅: {res['pct']:.2f}% (阈值:{PHASE_THRESH_PCT}%)\n"
           f"放量倍数: {res['vol_ratio']:.2f}x (阈值:{VOL_RATIO_THRESHOLD}x)\n"
           f"趋势背景: {res['trend']} ({res['ratio']:.0%})\n"
           f"收盘价格: {res['price']}")
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 确认信号: {symbol} P{phase}")
    asyncio.create_task(send_secure_push(f"{symbol} P{phase}", msg))

async def send_secure_push(title, content):
    if not SERVER_CHAN_KEY: return
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send", 
                               data={"title": title, "desp": content}, timeout=5)
        except: pass

# =====================================================
# 核心引擎：只听 K 线完成事件
# =====================================================
async def run_market_radar(client, symbols):
    bm = BinanceSocketManager(client)
    # 核心修改：只监听 kline_1m
    async with bm.multiplex_socket([f"{s.lower()}@kline_1m" for s in symbols]) as stream:
        while True:
            try:
                msg = await stream.recv()
                k_data = msg['data']['k']
                symbol = msg['data']['s']
                
                # 只有当这根 1m K 线【确认走完】时才记录并计算
                # 这保证了数据和你在 App 上看到的一模一样
                if k_data['x']: 
                    kline_vault[symbol].append({
                        'o': float(k_data['o']), 'h': float(k_data['h']),
                        'l': float(k_data['l']), 'c': float(k_data['c']), 'v': float(k_data['v'])
                    })
                    await on_kline_closed(symbol)
            except Exception as e:
                await asyncio.sleep(5)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        # 获取初选名单
        tickers = await client.futures_ticker()
        symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT") 
                   and float(t['quoteVolume']) > MIN_24H_VOLUME]
        
        # 必须预加载，否则趋势占比前100分钟无法计算
        print(f"开始预加载 {len(symbols)} 个币种的历史K线...")
        for s in symbols:
            kls = await client.futures_klines(symbol=s, interval='1m', limit=300)
            for k in kls:
                kline_vault[s].append({'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5])})
            await asyncio.sleep(0.02) # 防止频率过快

        # 分片运行
        shard_size = 25
        tasks = [run_market_radar(client, symbols[i:i+shard_size]) for i in range(0, len(symbols), shard_size)]
        print("🚀 核心雷达已锁定全市场，严格模式开启。")
        await asyncio.gather(*tasks)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    asyncio.run(main())
