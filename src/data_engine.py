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
    """读取 CSV 缓存，返回 DataFrame 或 None"""
    if os.path.exists(filepath):
        try:
            df = pd.read_csv(filepath, parse_dates=['date'])
            return df
        except Exception as e:
            logger.warning(f"缓存读取失败 {filepath}: {e}")
    return None


def _write_cache(df: pd.DataFrame, filepath: str):
    """写入 CSV 缓存"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    df.to_csv(filepath, index=False)


def _merge_and_write_cache(old_df: Optional[pd.DataFrame], new_df: pd.DataFrame, filepath: str):
    """合并新旧数据并写入缓存（去重）"""
    if old_df is not None and not old_df.empty:
        combined = pd.concat([old_df, new_df], ignore_index=True)
        combined.drop_duplicates(subset=['date'], keep='last', inplace=True)
        combined.sort_values('date', inplace=True)
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
    支持: hs300, zz500, top1500, all_filtered
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
    elif pool_name in ("top1500", "all_filtered"):
        # 获取全A股，然后按市值过滤
        rs = bs.query_all_stock(day=datetime.date.today().strftime("%Y-%m-%d"))
    else:
        raise ValueError(f"Unknown pool: {pool_name}")

    codes = []
    while rs.next():
        row = rs.get_row_data()
        if row[0]:  # 代码
            ts_code = _to_ts_code(row[0])
            codes.append(ts_code)

    if pool_name == "top1500":
        # 需要市值排序，这里简化处理：取前1500
        codes = codes[:1500]

    # 缓存股票池
    pd.DataFrame({'code': codes}).to_csv(pool_cache_file, index=False)
    logger.info(f"股票池 {pool_name}: {len(codes)} 只股票")
    return codes


# ============ 数据下载 ============

def _fetch_one_stock(bs_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """拉取单只股票日线数据（内部函数，带重试）"""
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
            return df
        except Exception as e:
            logger.warning(f"_fetch_one_stock 失败 (尝试 {attempt}/{MAX_RETRIES}): {e}，{RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]}s 后重试")
            _adaptive_sleep(success=False)
            time.sleep(RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)])
    logger.error(f"_fetch_one_stock 最终失败: {bs_code}")
    return None


def download_daily(codes: List[str], start_date: str, end_date: str,
                   use_cache: bool = True, incremental: bool = True) -> Dict[str, pd.DataFrame]:
    """
    批量下载日线数据（支持增量缓存）
    返回 {ts_code: DataFrame}
    """
    ensure_login()
    _ensure_cache_dirs()

    result = {}
    total = len(codes)
    cache_hits = 0
    api_calls = 0
    errors = 0

    # 断点续传
    progress = _load_progress("download_daily")
    completed_set = set(progress.get("completed", []))
    last_index = progress.get("last_index", 0)

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
            last_date = cached_df['date'].max()
            need_start = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if need_start > end_date:
                # 缓存已覆盖所需区间
                result[ts_code] = cached_df
                cache_hits += 1
                # 标记完成
                completed_set.add(ts_code)
                _save_progress("download_daily", {"completed": list(completed_set), "last_index": idx + 1, "total": total})
                continue
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
        _save_progress("download_daily", {"completed": list(completed_set), "last_index": idx + 1, "total": total})

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
            all_progress.pop("download_daily", None)
            with open(PROGRESS_FILE, 'w') as f:
                json.dump(all_progress, f, indent=2)
        except:
            pass

    logger.info(f"完成: 成功 {len(result)}/{total} | 缓存命中 {cache_hits} | "
                f"增量 {api_calls} | API调用 {api_calls} | 错误 {errors}")
    return result


def load_multi_stock_data(codes: List[str], start_date: str, end_date: str,
                          use_cache: bool = True, incremental: bool = True) -> Dict[str, pd.DataFrame]:
    """
    兼容原版接口名，与 download_daily 相同
    """
    return download_daily(codes, start_date, end_date, use_cache, incremental)


def download_index_daily(index_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """下载指数日线数据"""
    ensure_login()
    _ensure_cache_dirs()

    cache_path = os.path.join(INDEX_CACHE_DIR, f"{index_code.replace('.', '_')}.csv")
    cached_df = _read_cache(cache_path)

    # 增量逻辑
    if cached_df is not None and not cached_df.empty:
        last_date = cached_df['date'].max()
        need_start = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if need_start > end_date:
            return cached_df
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

    merged = _merge_and_write_cache(cached_df, df_new, cache_path)
    time.sleep(REQUEST_SLEEP)
    return merged


# ============ 其他工具函数 ============

def filter_by_liquidity(df: pd.DataFrame, min_amount: float = 1e8) -> pd.DataFrame:
    """按成交额过滤流动性不足的股票（每日）"""
    if 'amount' not in df.columns:
        return df
    return df[df['amount'] >= min_amount]


def get_stock_sectors(ts_code: str) -> Dict[str, str]:
    """获取股票所属行业（baostock 无申万行业，返回空）"""
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
