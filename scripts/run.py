"""
===========================================================
主入口 — 全A股 KDJ+量价多因子策略回测（baostock 增量缓存版）
===========================================================
用法:
 python run.py                              # 默认：全A股(剔除ST)，最近70天
 python run.py --pool all                    # 全A股（含ST）
 python run.py --pool all_filtered           # 全A股（剔除ST）★推荐
 python run.py --pool hs300                  # 沪深300（快速测试）
 python run.py --pool top20                  # 前20只（调试用）
 python run.py --start 20240101              # 自定义起始日期
 python run.py --no-plot                    # 不生成图表（更快）
 python run.py --no-incremental             # 禁用增量（强制全量重拉）

数据源：baostock（免费，无需 token）
"""

import sys
import time
import argparse
from pathlib import Path

# 确保能 import src 目录的模块
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from data_engine import (
    init_tushare, get_stock_pool, download_index_daily,
    load_multi_stock_data, download_daily, filter_by_liquidity,
    get_stock_sectors
)
from factor_engine import compute_all_factors
from backtest_engine import BacktestEngine
from visualizer import plot_full_report
import pandas as pd
import numpy as np

def main():
    parser = argparse.ArgumentParser(
        description='全A股 KDJ+量价多因子策略回测 (baostock 增量缓存版)'
    )
    parser.add_argument('--pool', default='all_filtered',
                        choices=['all', 'all_filtered', 'hs300', 'zz500',
                                 'top1500', 'top20'],
                        help='股票池 (默认: all_filtered=全A股剔除ST)')
    parser.add_argument('--start', default=None, help='起始日期 YYYYMMDD（默认70天前）')
    parser.add_argument('--end', default=None, help='结束日期 YYYYMMDD（默认今天）')
    parser.add_argument('--capital', type=float, default=1_000_000, help='初始资金')
    parser.add_argument('--no-plot', action='store_true', help='不生成图表')
    parser.add_argument('--execution', default='next_open',
                        choices=['close', 'next_open', 'next_vwap', 'next_close'],
                        help='成交模型 (默认: next_open)')
    parser.add_argument('--no-adaptive', action='store_true', help='禁用自适应参数')
    parser.add_argument('--no-batch', action='store_true', help='禁用分批建仓')
    parser.add_argument('--no-incremental', action='store_true', help='禁用增量缓存（强制全量）')
    parser.add_argument('--config', default=None, help='自定义配置JSON文件')
    parser.add_argument('--min-amount', type=float, default=20_000_000,
                        help='流动性过滤阈值（默认2000万/日）')
    args = parser.parse_args()

    # ============================================================
    # 0. 初始化
    # ============================================================
    print("=" * 65)
    print(" 全A股 KDJ+量价 多因子选股策略 — 回测系统")
    print(" 数据源: baostock（免费/无token）| 增量缓存 + 自适应限速")
    print("=" * 65)
    print(f" 启动时间: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")

    # 默认日期
    if args.end is None:
        args.end = pd.Timestamp.now().strftime('%Y%m%d')
    if args.start is None:
        args.start = (pd.Timestamp.now() - pd.Timedelta(days=70)).strftime('%Y%m%d')

    print(f" 数据区间: {args.start} ~ {args.end}")
    print(f" 股票池: {args.pool}")
    print()

    # baostock 初始化
    init_tushare()

    # ============================================================
    # 1. 获取股票池
    # ============================================================
    t0 = time.time()
    stock_list = get_stock_pool(args.pool)
    pool_time = time.time() - t0
    print(f" 股票池: {len(stock_list)} 只 (耗时 {pool_time:.1f}s)")

    if len(stock_list) == 0:
        print("[错误] 股票池为空，请检查网络或 baostock 连接")
        return

    # ============================================================
    # 2. 下载数据（增量模式）
    # ============================================================
    print(f"\n[数据下载] {args.start} ~ {args.end}")
    print(f" 模式: {'全量（禁用增量）' if args.no_incremental else '增量缓存（只拉缺失日期）'}")
    print(f" 预计: 全量~{len(stock_list)*0.5/60:.0f}分钟 / 增量~{len(stock_list)*0.5/60:.0f}分钟(1天)")
    print()

    t0 = time.time()
    stock_data = load_multi_stock_data(
        stock_list,
        args.start,
        args.end,
        progress=True,
        pool_key=args.pool  # 断点续传 key
    )
    data_time = time.time() - t0

    if len(stock_data) == 0:
        print("[错误] 未获取到任何股票数据，请检查网络或 baostock 连接")
        return

    print(f"\n 数据加载完成: {len(stock_data)}/{len(stock_list)} 只 (耗时 {data_time:.1f}s)")

    # 流动性过滤
    stock_data = filter_by_liquidity(stock_data, min_daily_amount=args.min_amount)

    # ============================================================
    # 3. 计算因子
    # ============================================================
    print(f"\n[因子计算] 开始...")
    valid_stocks = {}
    stock_list_sorted = sorted(stock_data.keys())

    t0 = time.time()
    for i, code in enumerate(stock_list_sorted):
        try:
            df = stock_data[code].copy()
            df_with_factors = compute_all_factors(df)
            valid_stocks[code] = df_with_factors
        except Exception as e:
            pass  # 静默跳过因子计算失败的股票

        if i % 200 == 199:
            elapsed = time.time() - t0
            print(f" 进度: {i+1}/{len(stock_data)} | 有效: {len(valid_stocks)} | {elapsed:.0f}s")

    factor_time = time.time() - t0
    print(f" 有效股票: {len(valid_stocks)}/{len(stock_data)} (耗时 {factor_time:.1f}s)")

    # ============================================================
    # 3.5 行业分类
    # ============================================================
    print(f"\n[行业分类] 加载数据...")
    stock_sectors = get_stock_sectors()
    if stock_sectors:
        covered = sum(1 for c in valid_stocks if c in stock_sectors)
        pct = covered / max(1, len(valid_stocks)) * 100
        print(f" 行业覆盖: {covered}/{len(valid_stocks)} ({pct:.0f}%)")
    else:
        print(" [信息] 行业数据为空，将跳过行业中性化（不影响核心策略）")

    # ============================================================
    # 4. 下载指数（用于择时）
    # ============================================================
    print(f"\n[指数数据] 下载上证指数...")
    try:
        index_df = download_index_daily('000001.SH', args.start, args.end)
        print(f" 上证指数: {len(index_df)} 条")
    except Exception as e:
        print(f" [警告] 指数下载失败: {e}")
        index_df = pd.DataFrame()

    # ============================================================
    # 5. 配置
    # ============================================================
    config = {
        # —— 资金与仓位 ——
        'initial_capital': args.capital,
        'max_positions': 4,
        'max_positions_low': 2,
        'risk_per_trade': 0.015,

        # —— 成交模型 ——
        'execution_model': 'next_open',
        'volume_participation_rate': 0.05,

        # —— 止损与止盈 ——
        'atr_stop_multiplier': 2.0,
        'trailing_stop_multiplier': 2.0,
        'trailing_activation': 0.04,
        'partial_profit_taking': True,
        'partial_profit_levels': [0.10, 0.20],
        'partial_exit_ratios': [0.25, 0.25],

        # —— 持仓时间 ——
        'max_hold_days': 25,
        'profit_deadline_days': 10,
        'profit_deadline_threshold': -0.02,
        'bbi_profit_protect_pct': 0.05,
        'bbi_profit_exit_ratio': 0.3,

        # —— 分批建仓 ——
        'batch_entry_enabled': True,
        'batch_entry_days': 2,
        'batch_entry_ratios': [0.5, 0.5],
        'batch_improvement_check': True,
        'batch_exit_enabled': True,
        'batch_exit_days': 2,
        'batch_exit_ratios': [0.5, 0.5],

        # —— 熔断与风控 ——
        'portfolio_stop_loss': 0.20,
        'max_sector_exposure': 0.40,
        'adx_trend_threshold': 25,
        'adaptive_params_enabled': True,

        # —— 选股 ——
        'j_threshold': 20,
        'j_60d_min_threshold': 12,
        'min_score': 50,
        'score_weights': {
            'oversold': 0.14,
            'volume_shrink': 0.13,
            'small_candle': 0.08,
            'prior_surge': 0.12,
            'reversal': 0.11,
            'bbi_trend': 0.10,
            'ma60_proximity': 0.07,
            'hot_industry': 0.12,
            'amihud': 0.03,
            'rebound_elasticity': 0.10,
        },
    }

    # 应用 CLI 参数覆盖
    config['execution_model'] = args.execution
    if args.no_adaptive:
        config['adaptive_params_enabled'] = False
    if args.no_batch:
        config['batch_entry_enabled'] = False
        config['batch_exit_enabled'] = False

    if args.config:
        import json
        with open(args.config, 'r') as f:
            custom = json.load(f)
        config.update(custom)
        print(f"\n[配置] 加载自定义配置: {args.config}")

    # ============================================================
    # 6. 运行回测
    # ============================================================
    print(f"\n[回测] 开始...")
    t0 = time.time()
    engine = BacktestEngine(config)
    daily_stats = engine.run(valid_stocks, index_df, stock_sectors=stock_sectors)
    bt_time = time.time() - t0

    if daily_stats is None or len(daily_stats) == 0:
        print("[错误] 回测无结果")
        return

    print(f" 回测完成 ({bt_time:.1f}s)")

    # ============================================================
    # 7. 计算基准收益（沪深300买入持有）
    # ============================================================
    benchmark_metrics = None
    benchmark_stats = None
    try:
        hs300_df = download_index_daily('000300.SH', args.start, args.end)
        if not hs300_df.empty:
            hs300_df['returns'] = hs300_df['close'].pct_change()
            hs300_df['equity'] = config['initial_capital'] * (
                1 + hs300_df['returns']
            ).cumprod()

            r = hs300_df['returns'].dropna()
            n_years = len(r) / 252
            total_ret = hs300_df['equity'].iloc[-1] / config['initial_capital'] - 1
            cagr = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
            ann_vol = r.std() * np.sqrt(252)
            cum = hs300_df['equity'] / config['initial_capital']
            max_dd = ((cum - cum.expanding().max()) / cum.expanding().max()).min()
            sharpe = (cagr - 0.03) / ann_vol if ann_vol > 0 else 0

            benchmark_metrics = {
                '总收益率': f"{total_ret:.2%}",
                '年化收益(CAGR)': f"{cagr:.2%}",
                '年化波动率': f"{ann_vol:.2%}",
                '夏普比率(Sharpe)': f"{sharpe:.3f}",
                '最大回撤': f"{max_dd:.2%}",
            }
            benchmark_stats = hs300_df
    except Exception as e:
        print(f" [警告] 基准计算失败: {e}")

    # ============================================================
    # 8. 打印报告
    # ============================================================
    engine.report(benchmark_metrics)

    # ============================================================
    # 9. 导出
    # ============================================================
    engine.export()

    # ============================================================
    # 10. 可视化
    # ============================================================
    if not args.no_plot:
        print("\n[可视化] 生成图表...")
        trades_df = pd.DataFrame(engine.trades) if engine.trades else None
        plot_full_report(
            daily_stats=engine.daily_stats,
            trades_df=trades_df,
            benchmark_stats=benchmark_stats,
            score_weights=config['score_weights'],
            save_path=Path(__file__).parent.parent / "output" / "backtest_report.png"
        )

    # ============================================================
    # 完成
    # ============================================================
    total_time = time.time() - start_time if 'start_time' in dir() else 0
    print("\n" + "=" * 65)
    print(f" 回测完成！查看 output/ 目录下的结果文件")
    print(f" 总耗时: {time.time() - t0_global:.0f}s")
    print("=" * 65)

if __name__ == "__main__":
    t0_global = time.time()
    main()
