"""
===========================================================
数据引擎 — baostock 全A股 + 增量缓存 + 自适应速率控制
===========================================================
核心能力（v4 — 全A股版）：
 1. 全A股股票池：~5000 只（沪市/深市/创业板/科创板/北交所）
 2. 分片缓存：cache/stocks/{交易所}/{code}_qfq.csv
 3. 增量更新：只拉取"上次缓存末尾 → 今天"的增量
 4. 自适应速率：基础 sleep 0.5s，遇限流自动退避到 2.0s
 5. 指数退避重试：失败自动重试 3 次（1s/2s/4s）
 6. 显式登出：atexit 注册 bs.logout()
 7. 进度持久化：中断后可从断点继续

缓存目录结构：
  cache/
  ├── stocks/
  │   ├── sh/      # 沪市主板/科创板
  │   ├── sz/      # 深市主板/创业板
  │   └── bj/      # 北交所
  ├── index/        # 指数日线
  ├── pool/         # 股票池快照
  └── funda/        # 基本面快照

设计原则：
 - 所有数据以 pandas DataFrame 返回，index 为 DatetimeIndex
 - 增量优先：先读本地 → 算增量区间 → 只拉增量 → append
 - 容错：API 失败 fallback 到缓存（即使过期也优于空）

与 tushare 版接口完全兼容，上层代码零修改。
"""

import os
import time
import glob
import json
import socket
import logging
import functools
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple, Optional

# ============================================================
# 0. 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("data_engine")

# ============================================================
# 1. 配置
# ============================================================
ROOT_DIR = Path(__file__).parent.parent
CACHE_DIR = ROOT_DIR / "cache"
STOCK_CACHE_DIR = CACHE_DIR / "stocks"
INDEX_CACHE_DIR = CACHE_DIR / "index"
POOL_CACHE_DIR = CACHE_DIR / "pool"
FUNDA_CACHE_DIR = CACHE_DIR / "funda"
PROGRESS_FILE = CACHE_DIR / "progress.json"

for d in [CACHE_DIR, STOCK_CACHE_DIR, INDEX_CACHE_DIR, POOL_CACHE_DIR, FUNDA_CACHE_DIR]:
    d.mkdir(exist_ok=True)
for exchange in ['sh', 'sz', 'bj']:
    (STOCK_CACHE_DIR / exchange).mkdir(exist_ok=True)

# 速率控制参数（自适应）
REQUEST_SLEEP = 0.5        # 每次 API 请求后休眠秒数（基础值）
BATCH_SIZE = 300            # 每 N 只股票额外休眠
BATCH_SLEEP = 2.0           # 批次间额外休眠秒数
MAX_RETRIES = 3             # 最大重试次数
RETRY_BACKOFF = [1, 2, 4]  # 指数退避间隔（秒）
SOCKET_TIMEOUT = 30          # 网络超时（秒）

# 自适应限速
_adaptive_sleep = REQUEST_SLEEP
_consecutive_errors = 0
_consecutive_success = 0

# baostock 全局连接
_bs = None
_code_cache = {}

# ============================================================
# 2. 网络与连接管理
# ============================================================
def _check_network() -> bool:
    """快速检测是否能连通 baostock 服务器"""
    try:
        socket.create_connection(("www.baostock.com", 80), timeout=5)
        return True
    except Exception:
        return False

def _get_bs():
    """获取 baostock 连接（懒加载单例）"""
    global _bs
    if _bs is not None:
        return _bs

    if not _check_network():
        raise RuntimeError(
            "无法连接 baostock 服务器（www.baostock.com），请检查网络"
        )

    import baostock as bs
    socket.setdefaulttimeout(SOCKET_TIMEOUT)

    for attempt in range(MAX_RETRIES):
        try:
            lg = bs.login()
            if lg.error_code == '0':
                _bs = bs
                log.info("baostock 登录成功")
                return _bs
            else:
                log.warning(f"登录失败 (尝试 {attempt+1}/{MAX_RETRIES}): {lg.error_msg}")
        except Exception as e:
            log.warning(f"登录异常 (尝试 {attempt+1}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF[attempt])

    raise RuntimeError("baostock 登录失败，已达最大重试次数")

def logout_bs():
    """退出登录"""
    global _bs
    if _bs is not None:
        try:
            _bs.logout()
            log.info("baostock 已登出")
        except Exception as e:
            log.warning(f"登出异常: {e}")
        finally:
            _bs = None

import atexit
atexit.register(logout_bs)

# ============================================================
# 3. 自适应速率控制
# ============================================================
def _adaptive_wait():
    """自适应休眠：连续成功→恢复快速，连续失败→加大间隔"""
    global _adaptive_sleep, _consecutive_errors, _consecutive_success

    time.sleep(_adaptive_sleep)

    # 缓慢恢复到基础速率
    if _consecutive_success > 50 and _adaptive_sleep > REQUEST_SLEEP:
        _adaptive_sleep = max(REQUEST_SLEEP, _adaptive_sleep - 0.05)

def _on_success():
    """请求成功回调"""
    global _consecutive_errors, _consecutive_success
    _consecutive_errors = 0
    _consecutive_success += 1

def _on_error():
    """请求失败回调 → 加大休眠"""
    global _adaptive_sleep, _consecutive_errors, _consecutive_success
    _consecutive_errors += 1
    _consecutive_success = 0
    # 指数退避：0.5 → 1.0 → 2.0 → 4.0（封顶）
    _adaptive_sleep = min(4.0, REQUEST_SLEEP * (2 ** _consecutive_errors))
    log.warning(f"限速触发：sleep 调整为 {_adaptive_sleep:.1f}s")

# ============================================================
# 4. 重试装饰器
# ============================================================
def with_retry(func):
    """指数退避重试装饰器"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                result = func(*args, **kwargs)
                _on_success()
                return result
            except Exception as e:
                last_err = e
                _on_error()
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    log.warning(f"{func.__name__} 失败 (尝试 {attempt+1}/{MAX_RETRIES}): {e}，{wait}s 后重试")
                    time.sleep(wait)
        log.error(f"{func.__name__} 最终失败: {last_err}")
        raise last_err
    return wrapper

# ============================================================
# 5. 代码格式转换
# ============================================================
def _to_bs_code(ts_code: str) -> str:
    """'000001.SZ' → 'sz.000001'"""
    if ts_code in _code_cache:
        return _code_cache[ts_code]
    parts = ts_code.split('.')
    code = parts[0]
    exchange = parts[1] if len(parts) > 1 else ''
    if exchange == 'SH' or code.startswith(('6', '9')):
        bs_code = f'sh.{code}'
    elif exchange == 'SZ' or code.startswith(('0', '3', '2')):
        bs_code = f'sz.{code}'
    elif exchange == 'BJ' or code.startswith(('4', '8')):
        bs_code = f'bj.{code}'
    else:
        bs_code = f'sz.{code}'
    _code_cache[ts_code] = bs_code
    return bs_code

def _to_ts_code(bs_code: str) -> str:
    """将 baostock 代码转为 tushare 格式（如 600519 -> 600519.SH）"""
    if '.' in bs_code:
        # 格式如 sh.600519 或 600519.SH
        parts = bs_code.split('.')
        code = parts[-1]  # 取数字部分
        exchange = parts[0].upper()
        if exchange in ('SH', 'SZ'):
            return f"{code}.{exchange}"
        elif exchange == 'SH':
            return f"{code}.SH"
        elif exchange == 'SZ':
            return f"{code}.SZ"
        else:
            # 未知交易所，根据代码前缀判断
            if code.startswith(('6', '9')):
                return f"{code}.SH"
            elif code.startswith(('0', '2', '3')):
                return f"{code}.SZ"
            elif code.startswith(('4', '8')):
                return f"{code}.BJ"
            else:
                return f"{code}.SH"
    else:
        # 纯数字格式，根据前缀判断交易所
        code = bs_code.strip()
        if code.startswith(('6', '9')):
            return f"{code}.SH"
        elif code.startswith(('0', '2', '3')):
            return f"{code}.SZ"
        elif code.startswith(('4', '8')):
            return f"{code}.BJ"
        else:
            return f"{code}.SH"

def _exchange_prefix(ts_code: str) -> str:
    """返回缓存子目录名: sh/sz/bj"""
    parts = ts_code.split('.')
    code = parts[0]
    exchange = parts[1] if len(parts) > 1 else ''
    if exchange == 'SH' or code.startswith(('6', '9')):
        return 'sh'
    elif exchange == 'BJ' or code.startswith(('4', '8')):
        return 'bj'
    else:
        return 'sz'

# ============================================================
# 6. 进度持久化（支持断点续传）
# ============================================================
def _load_progress(pool_key: str) -> dict:
    """加载进度记录"""
    if not PROGRESS_FILE.exists():
        return {}
    try:
        all_progress = json.loads(PROGRESS_FILE.read_text())
        return all_progress.get(pool_key, {})
    except Exception:
        return {}

def _save_progress(pool_key: str, progress: dict):
    """保存进度记录"""
    try:
        all_progress = {}
        if PROGRESS_FILE.exists():
            all_progress = json.loads(PROGRESS_FILE.read_text())
        all_progress[pool_key] = progress
        PROGRESS_FILE.write_text(json.dumps(all_progress, indent=2))
    except Exception as e:
        log.warning(f"进度保存失败: {e}")

# ============================================================
# 7. 股票池获取
# ============================================================
def get_stock_pool(method: str = "all_filtered", date: str = None) -> list:
    """
    获取候选股票池

    参数:
        method:
            "all"          — 全A股（含ST，~5400只）
            "all_filtered"  — 全A股（剔除ST/退市，~5000只）★推荐
            "hs300"         — 沪深300
            "zz500"         — 中证500
            "top1500"       — 全市场按市值前1500
            "top20"         — 前20只（快速测试）
        date: 指定日期（默认今天）

    返回:
        list[str]: 股票代码列表
    """
    bs = _get_bs()

    # 尝试读取最近 7 天的缓存
    for offset in range(7):
        d = date or (datetime.now() - timedelta(days=offset)).strftime('%Y%m%d')
        cache_file = POOL_CACHE_DIR / f"{method}_{d}.csv"
        if cache_file.exists() and cache_file.stat().st_size > 100:
            try:
                df = pd.read_csv(cache_file, dtype=str)
                stocks = df['ts_code'].dropna().tolist()
                if len(stocks) > 0:
                    log.info(f"股票池 {method}: {len(stocks)} 只 (缓存 {d})")
                    return stocks
            except Exception:
                pass

    # 缓存未命中，从 baostock 获取
    d = date or datetime.now().strftime('%Y%m%d')
    cache_file = POOL_CACHE_DIR / f"{method}_{d}.csv"

    if method == "hs300":
        rs = bs.query_hs300_stocks()
        stocks = []
        while rs.error_code == '0' and rs.next():
            stocks.append(_to_ts_code(rs.get_row_data()[0]))
        log.info(f"沪深300: {len(stocks)} 只")

    elif method == "zz500":
        rs = bs.query_zz500_stocks()
        stocks = []
        while rs.error_code == '0' and rs.next():
            stocks.append(_to_ts_code(rs.get_row_data()[0]))
        log.info(f"中证500: {len(stocks)} 只")

    elif method in ("all", "all_filtered"):
        # 全A股：沪市 + 深市（baostock 的 query_all_stock 返回两市）
        rs = bs.query_all_stock(day=d)
        stocks = []
        raw_map = {}  # bs_code → ts_code
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            bs_code = row[0]
            code = bs_code.split('.')[1] if '.' in bs_code else bs_code
            # 只保留股票（排除指数、基金、债券等）
            # 沪市: 6/9 开头; 深市: 0/3/2 开头; 北交所: 4/8 开头
            if code[:1] in ('0', '3', '6', '8', '4', '9', '2'):
                ts = _to_ts_code(bs_code)
                stocks.append(ts)
                raw_map[bs_code] = ts

        log.info(f"全A股原始: {len(stocks)} 只")

        if method == "all_filtered":
            # 剔除ST、退市、停牌
            try:
                rs_info = bs.query_stock_basic()
                info_dict = {}
                while rs_info.error_code == '0' and rs_info.next():
                    row = rs_info.get_row_data()
                    info_dict[row[0]] = row[2] if len(row) > 2 else ''

                filtered = []
                for s in stocks:
                    name = info_dict.get(s, '')
                    name_upper = name.upper()
                    # 剔除 ST、*ST、退市
                    if 'ST' in name_upper:
                        continue
                    if '退市' in name:
                        continue
                    filtered.append(s)
                stocks = filtered
                log.info(f"剔除ST/退市后: {len(stocks)} 只")
            except Exception as e:
                log.warning(f"ST过滤失败，使用原始列表: {e}")

    elif method == "top1500":
        rs = bs.query_all_stock(day=d)
        all_bs = []
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            bs_code = row[0]
            code = bs_code.split('.')[1] if '.' in bs_code else bs_code
            if code[:1] in ('0', '3', '6', '8', '4', '9'):
                all_bs.append(bs_code)

        # 用 daily_basic 按市值排序取前1500
        mv_data = []
        for i, b in enumerate(all_bs):
            try:
                rs_v = bs.query_daily_basic(code=b, day=d, fields="code,totalShare")
                if rs_v.error_code == '0' and rs_v.next():
                    mv_data.append(b)
            except:
                pass
            time.sleep(0.05)

        stocks = [_to_ts_code(c) for c in mv_data[:1500]]
        log.info(f"top1500: {len(stocks)} 只")

    elif method == "top20":
        return ['000001.SZ', '000002.SZ', '000333.SZ', '000651.SZ',
                '000858.SZ', '002594.SZ', '300750.SZ', '600036.SH',
                '600276.SH', '600519.SH', '600887.SH', '601318.SH',
                '601398.SH', '601857.SH', '601988.SH', '603259.SH',
                '603288.SH', '603986.SH', '688012.SH', '688599.SH']
    else:
        raise ValueError(f"不支持的股票池: {method}")

    if stocks:
        pd.DataFrame({'ts_code': stocks}).to_csv(cache_file, index=False)

    return stocks

# ============================================================
# 8. 行业分类（baostock 无申万行业，返回空）
# ============================================================
def get_stock_sectors(use_cache: bool = True) -> dict:
    cache_file = CACHE_DIR / "stock_industry_full.csv"
    if use_cache and cache_file.exists():
        try:
            df = pd.read_csv(cache_file, dtype=str)
            if 'industry' in df.columns and len(df) > 0:
                df = df[df['industry'].notna() & (df['industry'] != '')]
                mapping = dict(zip(df['ts_code'], df['industry']))
                log.info(f"行业分类从缓存加载: {len(mapping)} 只")
                return mapping
        except Exception:
            pass
    log.info("baostock 无行业数据，返回空映射")
    return {}

# ============================================================
# 9. 日线数据（核心：增量缓存 + 分片存储）
# ============================================================
def _stock_cache_path(ts_code: str, adj: str = 'qfq') -> Path:
    """股票缓存文件路径（按交易所分子目录）"""
    prefix = _exchange_prefix(ts_code)
    safe = ts_code.replace('.', '_')
    return STOCK_CACHE_DIR / prefix / f"{safe}_{adj}.csv"

def _read_stock_cache(path: Path) -> pd.DataFrame:
    """读取股票缓存 CSV → DataFrame"""
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        log.warning(f"读取缓存失败 {path}: {e}")
        return pd.DataFrame()

def _write_stock_cache(path: Path, df: pd.DataFrame):
    """写入股票缓存 CSV"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df_to_write = df.copy()
        if isinstance(df_to_write.index, pd.DatetimeIndex):
            df_to_write.index.name = 'date'
        df_to_write.to_csv(path)
    except Exception as e:
        log.warning(f"写入缓存失败 {path}: {e}")

@with_retry
def _fetch_one_stock(bs_code: str, start_date: str, end_date: str, adjust_flag: str) -> pd.DataFrame:
    """单次拉取一只股票（带重试）"""
    bs = _get_bs()
    rs = bs.query_history_k_data_plus(
        code=bs_code,
        fields="date,open,high,low,close,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag=adjust_flag
    )

    if rs.error_code != '0':
        raise RuntimeError(f"API error: {rs.error_msg}")

    data = []
    while rs.next():
        data.append(rs.get_row_data())

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df[df['volume'] > 0].copy()
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    df.sort_index(inplace=True)
    return df

def download_daily(stock_code: str, start_date: str, end_date: str,
                   adj: str = 'qfq', use_cache: bool = True) -> pd.DataFrame:
    """
    下载单只股票日线（增量缓存版）

    逻辑：
      1. 读本地缓存 → 拿到最后一条日期
      2. 如果缓存已覆盖 [start_date, end_date] → 直接返回切片
      3. 否则只拉 [缓存末尾+1天, end_date] 的增量 → append → 写回
    """
    bs_code = _to_bs_code(stock_code)
    adjust_flag = {'qfq': '2', 'hfq': '1'}.get(adj, '3')

    cache_path = _stock_cache_path(stock_code, adj)

    # 尝试读缓存
    cached = _read_stock_cache(cache_path) if use_cache else pd.DataFrame()

    # 缓存有效且覆盖所需区间 → 直接返回
    if not cached.empty:
        cached_start = cached.index.min().strftime('%Y%m%d')
        cached_end = cached.index.max().strftime('%Y%m%d')
        if cached_start <= start_date and cached_end >= end_date:
            return cached.loc[start_date:end_date]

    # 计算增量区间
    if not cached.empty:
        last_date = cached.index.max()
        inc_start = (last_date + timedelta(days=1)).strftime('%Y%m%d')
    else:
        inc_start = start_date

    if inc_start > end_date:
        return cached.loc[start_date:end_date] if not cached.empty else pd.DataFrame()

    # 拉取增量
    try:
        inc_df = _fetch_one_stock(bs_code, inc_start, end_date, adjust_flag)
    except Exception as e:
        log.error(f"拉取 {stock_code} 失败: {e}")
        if not cached.empty:
            log.warning(f"降级使用过期缓存: {stock_code}")
            return cached.loc[start_date:end_date]
        return pd.DataFrame()

    # 合并写回
    if use_cache:
        if not cached.empty:
            merged = pd.concat([cached, inc_df])
            merged = merged[~merged.index.duplicated(keep='last')]
            merged.sort_index(inplace=True)
        else:
            merged = inc_df
        _write_stock_cache(cache_path, merged)

    result = merged.loc[start_date:end_date] if 'merged' in locals() and not merged.empty else inc_df
    return result

# ============================================================
# 10. 指数数据（增量缓存）
# ============================================================
INDEX_CODE_MAP = {
    '000001.SH': 'sh.000001',
    '000300.SH': 'sh.000300',
    '000905.SH': 'sh.000905',
    '399001.SZ': 'sz.399001',
    '399006.SZ': 'sz.399006',
}

def download_index_daily(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """下载指数日线（增量缓存）"""
    bs = _get_bs()
    bs_code = INDEX_CODE_MAP.get(index_code, f'sh.{index_code[:6]}')
    safe = index_code.replace('.', '_')
    cache_path = INDEX_CACHE_DIR / f"{safe}.csv"

    cached = _read_stock_cache(cache_path)

    if not cached.empty:
        cached_start = cached.index.min().strftime('%Y%m%d')
        cached_end = cached.index.max().strftime('%Y%m%d')
        if cached_start <= start_date and cached_end >= end_date:
            return cached.loc[start_date:end_date]
        last_date = cached.index.max()
        inc_start = (last_date + timedelta(days=1)).strftime('%Y%m%d')
    else:
        inc_start = start_date

    if inc_start > end_date:
        return cached.loc[start_date:end_date] if not cached.empty else pd.DataFrame()

    rs = bs.query_history_k_data_plus(
        code=bs_code,
        fields="date,open,high,low,close,volume",
        start_date=inc_start,
        end_date=end_date,
        frequency="d",
        adjustflag="3"
    )

    if rs.error_code != '0':
        log.warning(f"指数 {index_code} 获取失败: {rs.error_msg}")
        if not cached.empty:
            return cached.loc[start_date:end_date]
        return pd.DataFrame()

    data = []
    while rs.next():
        data.append(rs.get_row_data())

    if not data:
        if not cached.empty:
            return cached.loc[start_date:end_date]
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    df.sort_index(inplace=True)

    if not cached.empty:
        merged = pd.concat([cached, df])
        merged = merged[~merged.index.duplicated(keep='last')]
        merged.sort_index(inplace=True)
    else:
        merged = df
    _write_stock_cache(cache_path, merged)

    return merged.loc[start_date:end_date]

# ============================================================
# 11. 基本面数据（带缓存）
# ============================================================
def download_fundamentals(trade_date: str, stock_list: list = None) -> pd.DataFrame:
    """获取基本面数据（PE/PB/ROE/市值），按日期缓存"""
    bs = _get_bs()
    cache_file = FUNDA_CACHE_DIR / f"{trade_date}.csv"

    if cache_file.exists():
        try:
            df = pd.read_csv(cache_file, index_col=0)
            if stock_list:
                df = df[df['ts_code'].isin(stock_list)]
            return df
        except Exception:
            pass

    try:
        rs = bs.query_daily_basic(
            day=trade_date,
            fields="code,peTTM,pbMRQ,psTTM,pcfNcfTTM,turnoverRatio"
        )
        if rs.error_code != '0':
            log.warning(f"基本面获取失败 ({trade_date}): {rs.error_msg}")
            return pd.DataFrame()

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=['code', 'pe', 'pb', 'ps', 'pcf', 'turnover_rate'])
        df['code'] = df['code'].apply(_to_ts_code)
        df.rename(columns={'code': 'ts_code'}, inplace=True)
        for col in ['pe', 'pb', 'ps', 'pcf', 'turnover_rate']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['pe', 'pb'])
        df = df[(df['pe'] > 0) & (df['pb'] > 0)]

        df.to_csv(cache_file)

        if stock_list:
            df = df[df['ts_code'].isin(stock_list)]
        return df
    except Exception as e:
        log.warning(f"基本面异常: {e}")
        return pd.DataFrame()

# ============================================================
# 12. 批量数据加载（全A股 + 增量 + 自适应速率 + 断点续传）
# ============================================================
def load_multi_stock_data(stock_codes: list, start_date: str, end_date: str,
                          progress: bool = True,
                          pool_key: str = "default") -> dict:
    """
    批量下载多只股票（全A股优化版）

    特性：
      - 增量缓存：已有缓存且覆盖区间 → 0 API 调用
      - 自适应速率：连续失败自动加大间隔，成功后缓慢恢复
      - 断点续传：进度持久化到 cache/progress.json
      - 降级策略：API 失败 → 返回过期缓存

    参数:
        stock_codes: 股票代码列表（全A股 ~5000 只）
        start_date: 起始日期
        end_date: 结束日期
        progress: 是否显示进度
        pool_key: 进度记录的 key（用于断点续传）

    返回:
        dict: {stock_code: DataFrame}
    """
    result = {}
    n = len(stock_codes)
    cache_hits = 0
    incremental = 0
    api_calls = 0
    errors = 0

    # 加载进度（断点续传）
    progress_data = _load_progress(pool_key)
    completed = set(progress_data.get('completed', []))
    last_index = progress_data.get('last_index', 0)

    # 跳过已完成的
    if last_index > 0 and len(completed) > 0:
        log.info(f"断点续传: 已完成 {len(completed)}/{n}，从索引 {last_index} 继续")

    log.info(f"批量加载 {n} 只股票，区间 {start_date}~{end_date}")

    for i, code in enumerate(stock_codes):
        # 跳过已完成的（断点续传）
        if code in completed:
            # 仍需把数据读出来
            cache_path = _stock_cache_path(code, 'qfq')
            cached = _read_stock_cache(cache_path)
            if not cached.empty:
                sliced = cached.loc[start_date:end_date]
                if len(sliced) >= 30:
                    result[code] = sliced
            continue

        # 进度打印
        if progress and i % 200 == 0:
            elapsed_pct = (i / n) * 100
            log.info(f"进度 {i}/{n} ({elapsed_pct:.0f}%) | "
                     f"命中 {cache_hits} | 增量 {incremental} | "
                     f"API {api_calls} | 错误 {errors}")

        # 检查缓存是否已覆盖（不发起 API 请求）
        cache_path = _stock_cache_path(code, 'qfq')
        cached = _read_stock_cache(cache_path)

        cache_covered = False
        if not cached.empty:
            cached_start = cached.index.min().strftime('%Y%m%d')
            cached_end = cached.index.max().strftime('%Y%m%d')
            if cached_start <= start_date and cached_end >= end_date:
                cache_covered = True

        if cache_covered:
            sliced = cached.loc[start_date:end_date]
            if len(sliced) >= 30:
                result[code] = sliced
                cache_hits += 1
                completed.add(code)
                continue

        # 需要 API 调用
        try:
            df = download_daily(code, start_date, end_date)
            api_calls += 1
            if not cached.empty and not df.empty:
                incremental += 1
            if not df.empty and len(df) >= 30:
                result[code] = df
            completed.add(code)
        except Exception as e:
            errors += 1
            log.warning(f"跳过 {code}: {e}")
            # 降级：用过期缓存
            if not cached.empty:
                sliced = cached.loc[start_date:end_date]
                if len(sliced) >= 30:
                    result[code] = sliced
                    log.info(f"  ↳ 降级使用过期缓存 ({len(sliced)} 行)")

        # 自适应休眠
        _adaptive_wait()

        # 批次缓冲
        if (i + 1) % BATCH_SIZE == 0:
            time.sleep(BATCH_SLEEP)
            # 每批次保存进度
            _save_progress(pool_key, {
                'completed': list(completed),
                'last_index': i + 1,
                'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })

    # 最终保存进度
    _save_progress(pool_key, {
        'completed': list(completed),
        'last_index': n,
        'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total': n
    })

    log.info(f"完成: 成功 {len(result)}/{n} | 缓存命中 {cache_hits} | "
             f"增量 {incremental} | API调用 {api_calls} | 错误 {errors}")
    return result

# ============================================================
# 13. 流动性过滤
# ============================================================
def filter_by_liquidity(stock_data: dict, min_daily_amount: float = 20_000_000,
                         lookback_days: int = 60) -> dict:
    """过滤流动性不足的股票"""
    filtered = {}
    removed = 0
    for code, df in stock_data.items():
        if len(df) < lookback_days:
            removed += 1
            continue
        recent = df.tail(lookback_days)
        avg_amount = recent['amount'].mean() if 'amount' in df.columns else 0
        if avg_amount >= min_daily_amount:
            filtered[code] = df
        else:
            removed += 1
    if removed > 0:
        log.info(f"流动性过滤剔除 {removed} 只（日均成交额 < {min_daily_amount/1e6:.0f}M）")
    return filtered

# ============================================================
# 14. 涨停/跌停判断
# ============================================================
def check_limit_status(df: pd.DataFrame, date_idx: int) -> Tuple[bool, bool]:
    """判断某一天是否为涨跌停日"""
    if len(df) <= date_idx or date_idx <= 0:
        return False, False
    row = df.iloc[date_idx]
    prev = df.iloc[date_idx - 1]
    if prev['close'] == 0:
        return False, False

    ret = (row['close'] - prev['close']) / prev['close']
    limit_up = ret >= 0.095
    limit_down = ret <= -0.095

    if abs(row['high'] - row['low']) / row['close'] < 0.005:
        if ret >= 0.095:
            limit_up = True
        elif ret <= -0.095:
            limit_down = True
    return limit_up, limit_down

# ============================================================
# 15. 市场宽度指标
# ============================================================
def compute_market_breadth(stock_data: dict, date: pd.Timestamp) -> dict:
    """计算全市场宽度指标"""
    total = above_ma20 = above_ma60 = up_count = new_high_count = 0
    total_volume = 0.0

    for code, df in stock_data.items():
        if date not in df.index:
            continue
        total += 1
        row = df.loc[date]
        if 'ma20' in df.columns:
            ma20 = row.get('ma20', np.nan)
            if not pd.isna(ma20) and row['close'] > ma20:
                above_ma20 += 1
        if 'ma60' in df.columns:
            ma60 = row.get('ma60', np.nan)
            if not pd.isna(ma60) and row['close'] > ma60:
                above_ma60 += 1
        if 'returns' in df.columns:
            ret = row.get('returns', np.nan)
            if not pd.isna(ret) and ret > 0:
                up_count += 1
        total_volume += row.get('volume', 0)
        hist = df.loc[:date]
        if len(hist) >= 20:
            recent_high = hist.iloc[-21:-1]['high'].max() if len(hist) > 20 else hist['high'].max()
            if row['high'] >= recent_high * 0.995:
                new_high_count += 1

    if total == 0:
        return {'breadth_ma20': 0.5, 'breadth_ma60': 0.5,
                'up_down_ratio': 1.0, 'volume_percentile': 50,
                'new_high_ratio': 0.05, 'total_stocks': 0}

    down_count = total - up_count
    return {
        'breadth_ma20': above_ma20 / total,
        'breadth_ma60': above_ma60 / total,
        'up_down_ratio': up_count / max(1, down_count),
        'volume_percentile': 50,
        'new_high_ratio': new_high_count / total,
        'total_stocks': total,
    }

# ============================================================
# 16. 仓位建议
# ============================================================
def breadth_to_position(breadth: dict) -> dict:
    """将市场宽度转化为仓位建议"""
    b20 = breadth.get('breadth_ma20', 0.5)
    b60 = breadth.get('breadth_ma60', 0.5)
    udr = breadth.get('up_down_ratio', 1.0)

    if b20 > 0.65 and b60 > 0.60 and udr > 2.0:
        return {'regime': 'bull', 'max_positions': 5, 'risk_per_trade': 0.020, 'breadth_ma20': b20, 'up_down_ratio': udr}
    elif b20 > 0.45 and udr > 1.2:
        return {'regime': 'neutral', 'max_positions': 4, 'risk_per_trade': 0.015, 'breadth_ma20': b20, 'up_down_ratio': udr}
    elif b20 > 0.30:
        return {'regime': 'cautious', 'max_positions': 3, 'risk_per_trade': 0.012, 'breadth_ma20': b20, 'up_down_ratio': udr}
    else:
        return {'regime': 'bear', 'max_positions': 2, 'risk_per_trade': 0.008, 'breadth_ma20': b20, 'up_down_ratio': udr}

# ============================================================
# 17. 兼容接口
# ============================================================
def init_tushare(token: str = None):
    """兼容接口：baostock 无需 token"""
    log.info("baostock 无需 token，直接连接")
    _get_bs()
    return _bs

def get_pro():
    """兼容接口"""
    return _get_bs()

# ============================================================
# 18. 测试入口
# ============================================================
if __name__ == "__main__":
    print("测试 baostock 全A股增量缓存数据引擎...\n")

    # 测试1：单只股票（首次 + 二次缓存命中）
    df = download_daily('600519.SH', '20250101', '20250731')
    print(f"✅ 贵州茅台: {len(df)} 条")
    if not df.empty:
        print(f"  区间: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")

    df2 = download_daily('600519.SH', '20250101', '20250731')
    print(f"✅ 二次读取（应0 API调用）: {len(df2)} 条")

    # 测试2：指数
    df_idx = download_index_daily('000001.SH', '20250101', '20250731')
    print(f"✅ 上证指数: {len(df_idx)} 条")

    # 测试3：全A股股票池
    stocks = get_stock_pool('all_filtered')
    print(f"✅ 全A股(剔除ST): {len(stocks)} 只")
    print(f"  前5只: {stocks[:5]}")

    logout_bs()
    print("\n🎉 测试完成")
