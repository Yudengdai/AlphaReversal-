#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据引擎 - baostock 版
支持增量缓存、自适应限速、全A股、断点续传
兼容原版 AlphaReversal 接口（load_multi_stock_data 等）
"""

import os
import sys
import time
import json
import atexit
import logging
import datetime
from typing import List, Dict, Optional, Tuple
from functools import lru_cache

import pandas as pd
import numpy as np
import baostock as bs

logger = logging.getLogger(__name__)

# ============ 全局配置 ============
CACHE_DIR = "cache"
STOCK_CACHE_DIR = os.path.join(CACHE_DIR, "stocks")
INDEX_CACHE_DIR = os.path.join(CACHE_DIR, "index")
POOL_CACHE_DIR = os.path.join(CACHE_DIR, "pool")
FUNDA_CACHE_DIR = os.path.join(CACHE_DIR, "funda")
PROGRESS_FILE = os.path.join(CACHE_DIR, "progress.json")

# 速率控制
REQUEST_SLEEP = 0.5          # 每次 API 调用后休眠（秒）
BATCH_SIZE = 200             # 每多少只股票额外休眠
BATCH_SLEEP = 2.0            # 批次间额外休眠（秒）
MAX_RETRIES = 3              # 最大重试次数
RETRY_BACKOFF = [1, 2, 4]    # 退避间隔（秒）

# 自适应限速
_current_sleep = REQUEST_SLEEP
_consecutive_failures = 0
_max_sleep = 4.0

# ============ 初始化 ============
_bs_logged_in = False

def ensure_login():
    """确保 baostock 已登录（单例）"""
    global _bs_logged_in
    if not _bs_logged_in:
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError(f"baostock 登录失败: {lg.error_msg}")
        _bs_logged_in = True
        logger.info("baostock 登录成功")
        atexit.register(bs.logout)

# ============ 代码格式转换 ============

def _to_ts_code(bs_code: str) -> str:
    """将 baostock 代码转为 tushare 格式（如 sh.600519 -> 600519.SH）"""
    if '.' in bs_code:
        parts = bs_code.split('.')
        code = parts[-1]
        exchange = parts[0].upper()
        if exchange == 'SH':
            return f"{code}.SH"
        elif exchange == 'SZ':
            return f"{code}.SZ"
        elif exchange == 'BJ':
            return f"{code}.BJ"
        else:
            # 根据代码前缀判断
            if code.startswith(('6', '9')):
                return f"{code}.SH"
            elif code.startswith(('0', '2', '3')):
                return f"{code}.SZ"
            elif code.startswith(('4', '8')):
                return f"{code}.BJ"
            else:
                return f"{code}.SH"
    else:
        code = bs_code.strip()
        if code.startswith(('6', '9')):
            return f"{code}.SH"
        elif code.startswith(('0', '2', '3')):
            return f"{code}.SZ"
        elif code.startswith(('4', '8')):
            return f"{code}.BJ"
        else:
            return f"{code}.SH"


def _to_bs_code(code: str) -> str:
    """将 tushare 格式代码（600519.SH）转为 baostock 格式（sh.600519）"""
    if '.' in code:
        sym, exchange = code.split('.')
        exchange = exchange.lower()
        return f"{exchange}.{sym}"
    else:
        sym = code.strip()
        if sym.startswith(('6', '9')):
            return f"sh.{sym}"
        elif sym.startswith(('0', '2', '3')):
            return f"sz.{sym}"
        elif sym.startswith(('4', '8')):
            return f"bj.{sym}"
        else:
            return f"sh.{sym}"


# ============ 缓存管理 ============

def _ensure_cache_dirs():
    """确保缓存目录存在"""
    for d in [STOCK_CACHE_DIR, INDEX_CACHE_DIR, POOL_CACHE_DIR, FUNDA_CACHE_DIR]:
        os.makedirs(d, exist_ok=True)


def _stock_cache_path(ts_code: str) -> str:
    """获取个股缓存文件路径（按交易所分目录）"""
    exchange = ts_code.split('.')[-1].lower()
    sub_dir = os.path.join(STOCK_CACHE_DIR, exchange)
    os.makedirs(sub_dir, exist_ok=True)
    safe_name = ts_code.replace('.', '_')
    return os.path.join(sub_dir, f"{safe_name}.csv")


def _read_cache(filepath: str) -> Optional[pd.DataFrame]:
    """读取 CSV 缓存，返回带日期索引的 DataFrame 或 None"""
    if os.path.exists(filepath):
        try:
            df = pd.read_csv(filepath, parse_dates=['date'])
            if df.empty:
                return None
            df.set_index('date', inplace=True)
            return df
        except Exception as e:
            logger.warning(f"缓存读取失败 {filepath}: {e}")
    return None


def _write_cache(df: pd.DataFrame, filepath: str):
    """写入 CSV 缓存（date 索引转为列存储，便于兼容与调试）"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    out = df.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        out = out.reset_index()  # date 索引 → date 列
    out.to_csv(filepath, index=False)


def _merge_and_write_cache(old_df: Optional[pd.DataFrame], new_df: pd.DataFrame, filepath: str):
    """合并新旧数据并写入缓存（按日期索引去重）"""
    if old_df is not None and not old_df.empty:
        combined = pd.concat([old_df, new_df])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined.sort_index(inplace=True)
    else:
        combined = new_df.copy()
    _write_cache(combined, filepath)
    return combined


# ============ 断点续传 ============

def _load_progress(pool_name: str) -> dict:
    """加载断点进度"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                all_progress = json.load(f)
                return all_progress.get(pool_name, {})
        except:
            pass
    return {"completed": [], "last_index": 0, "total": 0}


def _save_progress(pool_name: str, progress: dict):
    """保存断点进度"""
    all_progress = {}
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                all_progress = json.load(f)
        except:
            pass
    all_progress[pool_name] = progress
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(all_progress, f, indent=2)


# ============ 自适应限速 ============

def _adaptive_sleep(success: bool):
    """根据成功/失败调整休眠时间"""
    global _current_sleep, _consecutive_failures
    if success:
        _consecutive_failures = 0
        # 缓慢恢复至基础值
        _current_sleep = max(REQUEST_SLEEP, _current_sleep - 0.05)
    else:
        _consecutive_failures += 1
        # 指数退避
        idx = min(_consecutive_failures - 1, len(RETRY_BACKOFF) - 1)
        _current_sleep = min(_max_sleep, RETRY_BACKOFF[idx])
        logger.warning(f"限速触发：sleep 调整为 {_current_sleep}s")

    time.sleep(_current_sleep)


# ============ 股票池 ============

def get_stock_pool(pool_name: str = "hs300") -> List[str]:
    """
    获取股票池（返回 tushare 格式代码列表）
    支持: hs300, zz500, top1500, all_filtered, all, top20

    注意：
    - hs300/zz500 用 baostock 指数成分接口
    - all/all_filtered/top1500/top20 用 query_stock_basic（type=1 才是股票，
      排除指数/转债/ETF；query_all_stock 会混入指数，不可用）
    """
    ensure_login()
    _ensure_cache_dirs()

    # 优先使用缓存（7天内有效）
    pool_cache_file = os.path.join(POOL_CACHE_DIR, f"{pool_name}.csv")
    if os.path.exists(pool_cache_file):
        mtime = os.path.getmtime(pool_cache_file)
        if (time.time() - mtime) < 7 * 86400:
            df = pd.read_csv(pool_cache_file)
            if 'code' in df.columns:
                return df['code'].tolist()

    if pool_name == "hs300":
        rs = bs.query_hs300_stocks()
    elif pool_name == "zz500":
        rs = bs.query_zz500_stocks()
    elif pool_name in ("all", "all_filtered", "top1500", "top20"):
        # 全A股：query_stock_basic 的 type=1 才是股票（排除指数/转债/ETF）
        rs = bs.query_stock_basic()
    else:
        raise ValueError(f"Unknown pool: {pool_name}")

    codes = []
    while rs.next():
        row = rs.get_row_data()
        if not row or not row[0]:  # 代码
            continue
        # 全A股池：只保留 type=1 的股票
        if pool_name in ("all", "all_filtered", "top1500", "top20"):
            if len(row) <= 4 or row[4] != '1':
                continue  # 指数(2)/转债(4)/ETF(5)等全部排除
            # all_filtered：剔除名称含 ST / 退 的股票
            if pool_name == "all_filtered":
                name = row[1] if len(row) > 1 and row[1] else ""
                if "ST" in name.upper() or "退" in name:
                    continue
            raw_code = row[0]
        else:
            # hs300/zz500：返回格式为 [updateDate, code, code_name]，code 在第 1 列
            raw_code = row[1]
        ts_code = _to_ts_code(raw_code)
        codes.append(ts_code)

    if pool_name == "top1500":
        # 简化处理：取前1500（未真正按市值排序，市值排序需额外接口）
        codes = codes[:1500]
    elif pool_name == "top20":
        # 调试用：优先用中证500成分股（存活股，避免 query_stock_basic 里的退市代码）
        try:
            rs = bs.query_zz500_stocks()
            live = []
            while rs.next() and len(live) < 20:
                row = rs.get_row_data()
                if row and len(row) > 1 and row[1]:
                    live.append(_to_ts_code(row[1]))
            if live:
                codes = live[:20]
        except Exception as e:
            logger.warning(f"top20 改用 zz500 成分失败: {e}，回退前20只")
            codes = codes[:20]

    # 缓存股票池
    pd.DataFrame({'code': codes}).to_csv(pool_cache_file, index=False)
    logger.info(f"股票池 {pool_name}: {len(codes)} 只股票")
    return codes


# ============ 数据下载 ============

def _normalize_date(d: str) -> str:
    """将 YYYYMMDD 统一转为 YYYY-MM-DD（baostock 要求带横线格式）"""
    if not d:
        return d
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def _fetch_one_stock(bs_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """拉取单只股票日线数据（内部函数，带重试）"""
    start_date = _normalize_date(start_date)
    end_date = _normalize_date(end_date)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,preclose,volume,amount,turn,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2"  # 后复权
            )
            rows = []
            while rs.next():
                row = rs.get_row_data()
                if row[0]:  # 有数据
                    rows.append(row)
            if not rows:
                return pd.DataFrame()
            columns = ['date', 'open', 'high', 'low', 'close', 'preclose',
                       'volume', 'amount', 'turn', 'pctChg']
            df = pd.DataFrame(rows, columns=columns)
            for col in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)  # 统一日期索引（backtest/factor 均按日期索引访问）
            return df
        except Exception as e:
            logger.warning(f"_fetch_one_stock 失败 (尝试 {attempt}/{MAX_RETRIES}): {e}，{RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]}s 后重试")
            _adaptive_sleep(success=False)
            time.sleep(RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)])
    logger.error(f"_fetch_one_stock 最终失败: {bs_code}")
    return None


def download_daily(codes: List[str], start_date: str, end_date: str,
                   use_cache: bool = True, incremental: bool = True,
                   progress: bool = True, pool_key: str = "download_daily") -> Dict[str, pd.DataFrame]:
    """
    批量下载日线数据（支持增量缓存）
    返回 {ts_code: DataFrame}

    progress: 是否启用断点续传（默认 True）
    pool_key: 断点续传的 key（不同股票池分开记，默认 download_daily）
    """
    ensure_login()
    _ensure_cache_dirs()

    # 统一日期格式为 YYYY-MM-DD（避免 baostock 报"日期格式不正确"）
    start_date = _normalize_date(start_date)
    end_date = _normalize_date(end_date)

    result = {}
    total = len(codes)
    cache_hits = 0
    api_calls = 0
    errors = 0

    # 断点续传
    progress_state = _load_progress(pool_key) if progress else {}
    completed_set = set(progress_state.get("completed", []))
    last_index = progress_state.get("last_index", 0)

    for idx, ts_code in enumerate(codes):
        if idx < last_index and ts_code in completed_set:
            # 已完成的直接跳过（但需要从缓存加载）
            cache_path = _stock_cache_path(ts_code)
            df = _read_cache(cache_path)
            if df is not None and not df.empty:
                result[ts_code] = df
                cache_hits += 1
            continue

        cache_path = _stock_cache_path(ts_code)
        cached_df = _read_cache(cache_path) if use_cache else None

        # 确定需要拉取的日期范围
        if incremental and cached_df is not None and not cached_df.empty:
            cache_start = cached_df.index.min()
            if start_date >= cache_start.strftime("%Y-%m-%d"):
                # 新请求起点不早于缓存起点：增量拉尾部
                last_date = cached_df.index.max()
                need_start = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                if need_start > end_date:
                    # 缓存已覆盖所需区间
                    result[ts_code] = cached_df
                    cache_hits += 1
                    # 标记完成
                    completed_set.add(ts_code)
                    _save_progress(pool_key, {"completed": list(completed_set), "last_index": idx + 1, "total": total})
                    continue
            else:
                # 新请求起点更早：全量重拉（覆盖更早区间）
                need_start = start_date
        else:
            need_start = start_date

        # 调用 baostock
        bs_code = _to_bs_code(ts_code)
        df_new = _fetch_one_stock(bs_code, need_start, end_date)
        api_calls += 1

        if df_new is None or df_new.empty:
            # 降级：使用旧缓存
            if cached_df is not None:
                result[ts_code] = cached_df
                logger.warning(f"降级使用旧缓存: {ts_code}")
            else:
                errors += 1
                logger.error(f"拉取 {ts_code} 失败且无缓存")
        else:
            # 合并缓存
            merged = _merge_and_write_cache(cached_df, df_new, cache_path)
            result[ts_code] = merged

        # 标记完成
        completed_set.add(ts_code)
        _save_progress(pool_key, {"completed": list(completed_set), "last_index": idx + 1, "total": total})

        # 进度日志
        if (idx + 1) % 50 == 0 or idx == total - 1:
            logger.info(f"进度 {idx+1}/{total} ({((idx+1)/total)*100:.0f}%) | "
                        f"命中 {cache_hits} | 增量 {api_calls} | API {api_calls} | 错误 {errors}")

        # 速率控制
        _adaptive_sleep(success=(df_new is not None))

        # 批次休眠
        if (idx + 1) % BATCH_SIZE == 0:
            time.sleep(BATCH_SLEEP)

    # 清理进度文件（下载完成）
    if os.path.exists(PROGRESS_FILE):
        try:
            all_progress = json.load(open(PROGRESS_FILE))
            all_progress.pop(pool_key, None)
            with open(PROGRESS_FILE, 'w') as f:
                json.dump(all_progress, f, indent=2)
        except:
            pass

    logger.info(f"完成: 成功 {len(result)}/{total} | 缓存命中 {cache_hits} | "
                f"增量 {api_calls} | API调用 {api_calls} | 错误 {errors}")
    return result


def load_multi_stock_data(codes: List[str], start_date: str, end_date: str,
                          use_cache: bool = True, incremental: bool = True,
                          progress: bool = True, pool_key: str = "download_daily") -> Dict[str, pd.DataFrame]:
    """
    兼容原版接口名，与 download_daily 相同（支持断点续传参数）
    """
    return download_daily(codes, start_date, end_date, use_cache, incremental,
                          progress=progress, pool_key=pool_key)


def download_index_daily(index_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    start_date = _normalize_date(start_date)
    end_date = _normalize_date(end_date)

    """下载指数日线数据"""
    ensure_login()
    _ensure_cache_dirs()

    cache_path = os.path.join(INDEX_CACHE_DIR, f"{index_code.replace('.', '_')}.csv")
    cached_df = _read_cache(cache_path)

    # 增量逻辑
    if cached_df is not None and not cached_df.empty:
        cache_start = cached_df.index.min()
        last_date = cached_df.index.max()
        if start_date >= cache_start.strftime("%Y-%m-%d"):
            # 新请求起点不早于缓存起点：增量拉缺失尾部
            need_start = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if need_start > end_date:
                return cached_df
        else:
            # 新请求起点更早：全量重拉（覆盖更早区间）
            need_start = start_date
    else:
        need_start = start_date

    bs_code = _to_bs_code(index_code)
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount",
        start_date=need_start,
        end_date=end_date,
        frequency="d"
    )
    rows = []
    while rs.next():
        row = rs.get_row_data()
        if row[0]:
            rows.append(row)
    if not rows:
        return cached_df  # 降级

    columns = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    df_new = pd.DataFrame(rows, columns=columns)
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        df_new[col] = pd.to_numeric(df_new[col], errors='coerce')
    df_new['date'] = pd.to_datetime(df_new['date'])
    df_new.set_index('date', inplace=True)  # 统一日期索引

    merged = _merge_and_write_cache(cached_df, df_new, cache_path)
    time.sleep(REQUEST_SLEEP)
    return merged


# ============ 其他工具函数 ============

def is_price_limit_day(df: pd.DataFrame, idx: int, ts_code: str = '') -> tuple:
    """
    判断某天是否涨/跌停（用于交易保护）。
    A股涨跌停幅度：主板/中小板 10%，创业板(300)/科创板(688) 20%，北交所(8/4开头) 30%。
    根据代码前缀判断，用 close vs preclose 计算实际涨跌幅。
    返回 (is_limit_up, is_limit_down)
    """
    if df is None or df.empty or idx < 1:
        return False, False
    if idx >= len(df):
        idx = len(df) - 1
    code = str(ts_code or '')
    if code.startswith(('300', '301', '688', '689')):
        limit_pct = 0.20
    elif code.startswith(('8', '4', '92')):
        limit_pct = 0.30
    else:
        limit_pct = 0.10

    close = df['close'].iloc[idx]
    preclose = df['preclose'].iloc[idx] if 'preclose' in df.columns else df['close'].iloc[idx - 1]
    if preclose is None or pd.isna(preclose) or preclose <= 0:
        return False, False
    pct = (close - preclose) / preclose
    # 允许 ±0.5% 容差（四舍五入到分后可能略低于名义幅度）
    return pct >= limit_pct - 0.005, pct <= -(limit_pct - 0.005)


def filter_by_liquidity(data: Dict[str, pd.DataFrame], min_daily_amount: float = 2e7) -> Dict[str, pd.DataFrame]:
    """
    按日均成交额过滤流动性不足的股票

    参数:
        data: {ts_code: DataFrame}，DataFrame 需含 amount 列
        min_daily_amount: 日均成交额下限（元），默认 2000 万

    返回:
        过滤后的 {ts_code: DataFrame}
    """
    result = {}
    for ts_code, df in data.items():
        if df is None or df.empty:
            continue
        if 'amount' not in df.columns:
            # 没有成交额字段，保守保留（无法判断流动性）
            result[ts_code] = df
            continue
        avg_amount = df['amount'].mean()
        if avg_amount >= min_daily_amount:
            result[ts_code] = df
        else:
            logger.info(f"流动性过滤剔除: {ts_code} (日均成交额 {avg_amount/1e4:.0f}万 < {min_daily_amount/1e4:.0f}万)")
    return result


def get_stock_sectors(ts_code: str = None) -> Dict[str, str]:
    """
    获取股票所属行业（baostock 无申万行业数据，返回空）
    兼容两种调用：get_stock_sectors() 全量 或 get_stock_sectors(code) 单只
    """
    return {}


def download_fundamentals(codes: List[str], trade_date: str) -> Dict[str, Dict]:
    """
    下载基本面数据（baostock 的 query_daily_basic）
    返回 {ts_code: {pe, pb, ...}}
    """
    ensure_login()
    result = {}
    for ts_code in codes:
        bs_code = _to_bs_code(ts_code)
        rs = bs.query_daily_basic(bs_code, trade_date, "peTTM,pbMRQ,psTTM,pcfNcfTTM")
        while rs.next():
            row = rs.get_row_data()
            if row[0]:
                result[ts_code] = {
                    'pe': float(row[0]) if row[0] else None,
                    'pb': float(row[1]) if row[1] else None,
                    'ps': float(row[2]) if row[2] else None,
                    'pcf': float(row[3]) if row[3] else None,
                }
        time.sleep(REQUEST_SLEEP)
    return result


def init_tushare(token: str = None):
    """
    兼容原版接口名，实际不做任何事（baostock 无需 token）
    返回一个假的 pro 对象，使上层代码不报错
    """
    class FakePro:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None
    return FakePro()


# ============ 测试入口 ============
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    codes = get_stock_pool("hs300")
    logger.info(f"获取到 {len(codes)} 只股票")
    data = download_daily(codes[:5], "20260701", "20260731")
    for code, df in data.items():
        logger.info(f"{code}: {len(df)} 行")
