import asyncio
import time
import aiohttp
import pandas as pd
from collections import deque, defaultdict
from datetime import datetime
from binance import AsyncClient, BinanceSocketManager

# =====================================================
# 配置参数
# =====================================================
API_KEY = ""
API_SECRET = ""
SERVER_CHAN_KEY = "sctp14659thuntd89pzhhlsmbwynooxu"

MIN_24H_VOLUME = 8_000_000  # 24h成交额筛选
PHASE_THRESH_PCT = 0.6      # 涨幅阈值
VOL_RATIO_THRESHOLD = 1.5   # 放量倍数
EMA_PERIOD = 144            # 均线周期
TREND_WINDOW = 100          # 趋势统计窗口
TREND_RATIO = 0.7           # 占比阈值（70%时间在上方为看涨）

PHASE_COOLDOWN = 60         # 同币种同Phase冷却(秒)
KLINE_MAX = 300             # 内存保留K线数量

# =====================================================
# 全局数据
# =====================================================
kline_data = defaultdict(lambda: deque(maxlen=KLINE_MAX))
last_push_time = {}

# =====================================================
# 趋势判定引擎：K线运行占比逻辑
# =====================================================
def get_trend_quality(symbol):
    data = list(kline_data[symbol])
    if len(data) < (EMA_PERIOD + TREND_WINDOW):
        return "初始化中", 0
    
    df = pd.DataFrame(data)
    # 计算 EMA144
    ema = df['c'].ewm(span=EMA_PERIOD, adjust=False).mean()
    
    # 取最后 100 根进行占比统计
    last_100_c = df['c'].tail(TREND_WINDOW)
    last_100_ema = ema.tail(TREND_WINDOW)
    
    # 计算在均线上方的 K 线数量
    above_count = (last_100_c > last_100_ema).sum()
    ratio = above_count / TREND_WINDOW

    if ratio >= TREND_RATIO:
        return "看涨", ratio
    elif ratio <= (1 - TREND_RATIO):
        return "看跌", ratio
    else:
        return "震荡", ratio

# =====================================================
# 核心计算与信号匹配
# =====================================================
async def check_signal(symbol):
    trend, ratio = get_trend_quality(symbol)
    
    # 获取最新一根K线数据计算涨幅和放量
    df = pd.DataFrame(list(kline_data[symbol]))
    last = df.iloc[-1]
    vol_ma20 = df['v'].rolling(20).mean().iloc[-1]
    
    pct = (last['c'] - last['o']) / last['o'] * 100
    vol_ratio = last['v'] / (vol_ma20 + 1e-9)

    # 判定 Phase
    phase = 0
    if abs(pct) >= PHASE_THRESH_PCT * 1.5:
        phase = 4
    elif abs(pct) >= PHASE_THRESH_PCT and vol_ratio >= VOL_RATIO_THRESHOLD:
        phase = 2
    
    if phase == 0: return

    # 冷却与重复检查
    now = time.time()
    if now - last_push_time.get((symbol, phase), 0) < PHASE_COOLDOWN:
        return

    last_push_time[(symbol, phase)] = now
    
    # 构造推送内容
    msg = (f"币对: {symbol} | Phase {phase}\n"
           f"趋势状态: {trend} (占比:{ratio:.2%})\n"
           f"当前涨幅: {pct:.2f}%\n"
           f"放量比率: {vol_ratio:.2f}x\n"
           f"实时价格: {last['c']}\n"
           f"时间: {datetime.now().strftime('%M:%S')}")
    
    print(f"核心信号触发: {symbol} P{phase} | {trend}")
    asyncio.create_task(push_to_server_chan(f"{symbol} P{phase}", msg))

async def push_to_server_chan(title, content):
    if not SERVER_CHAN_KEY: return
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, data={"title": title, "desp": content}, timeout=5)
        except: pass

# =====================================================
# 数据预热：防止启动时的指标虚假
# =====================================================
async def preload_data(client, symbols):
    print(f"⏳ 正在为 {len(symbols)} 个币种预加载 300 根历史K线...")
    for s in symbols:
        try:
            klines = await client.futures_klines(symbol=s, interval='1m', limit=300)
            for k in klines:
                kline_data[s].append({'o':float(k[1]), 'h':float(k[2]), 'l':float(k[3]), 'c':float(k[4]), 'v':float(k[5])})
        except Exception as e:
            print(f"预加载 {s} 失败: {e}")
        await asyncio.sleep(0.05) # 避开频率限制
    print("✅ 预加载完成，指标已校准。")

# =====================================================
# WebSocket 接收引擎
# =====================================================
async def run_shard(client, symbols):
    bm = BinanceSocketManager(client)
    # 使用 miniTicker 获取每秒最新的价格和成交量聚合
    async with bm.multiplex_socket([f"{s.lower()}@miniTicker" for s in symbols]) as stream:
        while True:
            try:
                msg = await stream.recv()
                res = msg.get('data', {})
                s = res.get('s')
                if s:
                    # 实时模拟 1m K线更新
                    kline_data[s].append({
                        'o': float(res['o']), 'h': float(res['h']),
                        'l': float(res['l']), 'c': float(res['c']), 'v': float(res['v'])
                    })
                    await check_signal(s)
            except Exception as e:
                print(f"WS分片运行异常: {e}")
                await asyncio.sleep(5)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        # 获取市场列表
        info = await client.futures_ticker()
        symbols = [t['symbol'] for t in info if t['symbol'].endswith("USDT") 
                   and float(t['quoteVolume']) > MIN_24H_VOLUME]
        
        await preload_data(client, symbols)
        
        # 分片启动（每20个币一个Task）
        shard_size = 20
        tasks = [run_shard(client, symbols[i:i+shard_size]) for i in range(0, len(symbols), shard_size)]
        print(f"🚀 系统已上线，监控中...")
        await asyncio.gather(*tasks)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    asyncio.run(main())
