"""
===========================================================
回测引擎 — 向量化多资产回测 + 绩效分析
===========================================================
职责：
  1. 多空选股排名 → 每日调仓
  2. ATR 动态止损
  3. 波动率目标仓位管理
  4. 交易成本模拟（佣金+印花税+滑点）
  5. 绩效指标计算（Sharpe/Sortino/Calmar/MAR）
  6. 交易记录 + 归因分析

区别于原版 B1.py 的优化：
  - 原版止损固定10% → ATR 2x 动态止损
  - 原版仓位固定金额 → 波动率目标仓位
  - 原版无市场状态判断 → ADX 趋势过滤
  - 原版仅基本日志 → 完整的绩效归因体系
  - 新增最大回撤熔断机制
  - 新增行业集中度限制
  - 新增 Walk-forward 验证框架

面试考点：
  Q: 向量化回测和事件驱动回测的区别？
  A: 向量化 = 用矩阵运算一次性算完所有信号和持仓（pandas）
     事件驱动 = 逐条 bar 遍历，模拟真实交易流程（backtrader）
     向量化优势：快（500只×10年=秒级），适合选股策略
     事件驱动优势：真实（支持限价单/冰山指令），适合作市策略
     面试时两种都要能做。

  Q: 回测的常见陷阱？
  A: ① 前视偏差：用今天收盘价决定今天买入 → 用 .shift(1)
     ② 幸存者偏差：回测池包含已退市股票 → 使用历史成分股
     ③ 过拟合：参数在样本内调最优 → 必须留 OOS 验证
     ④ 交易成本低估：忽略滑点/冲击成本 → 保守估计滑点
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')


# ============================================================
# 0. 成交价格模拟器
# ============================================================

class ExecutionSimulator:
    """
    模拟真实成交价格，消除前视偏差 (Look-ahead Bias)

    问题：
      回测中信号基于 T 日收盘价计算，但实际交易最早只能在 T+1 日执行。
      直接用 T 日收盘价成交 = 前视偏差（你不可能在收盘前就知道收盘价）。

    方案：
      - 信号检测：T 日收盘后（使用 T 日 OHLCV 数据）
      - 订单执行：T+1 日（使用 T+1 日开盘价/均价）

    支持的成交模型：
      'next_open'   → T+1 开盘价（最保守，推荐默认值）
      'next_vwap'   → T+1 (O+H+L+C)/4（模拟 VWAP 成交）
      'close'       → T 日收盘价（旧行为，有前视偏差，仅用于对比）
      'next_close'  → T+1 收盘价（乐观估计）

    面试考点：
      Q: 回测中如何避免前视偏差？
      A: ① 信号计算用 T 日收盘价（.shift(1) 滞后一天）
         ② 成交价格用 T+1 日开盘价（最早可执行时间点）
         ③ 停牌日/涨跌停日不能交易
         ④ 大单需要考虑成交量约束（volume participation rate）
    """

    def __init__(self, model: str = 'next_open', participation_rate: float = 0.05):
        """
        参数:
            model:              成交模型 'next_open' | 'next_vwap' | 'close' | 'next_close'
            participation_rate: 最大成交量参与率（单日最多占当日成交的 5%）
        """
        self.model = model
        self.participation_rate = participation_rate

    def get_exec_price(self, df: pd.DataFrame, date_idx: int,
                       model: str = None) -> Optional[float]:
        """
        获取指定日期的成交价格

        参数:
            df:       股票日线 DataFrame（含 open/high/low/close/volume）
            date_idx: 信号触发日在 df 中的位置索引
            model:    覆盖默认成交模型

        返回:
            成交价格（float），数据不足返回 None

        逻辑：
          - 'close':      返回 date_idx 的收盘价（旧行为）
          - 'next_open':  返回 date_idx+1 的开盘价
          - 'next_vwap':  返回 date_idx+1 的 (O+H+L+C)/4
          - 'next_close': 返回 date_idx+1 的收盘价
        """
        model = model or self.model

        if model == 'close':
            if date_idx < len(df):
                return float(df['close'].iloc[date_idx])
            return None

        # T+1 模型：需要下一个交易日
        next_idx = date_idx + 1
        if next_idx >= len(df):
            return None  # 最后一天无 T+1

        row = df.iloc[next_idx]

        if model == 'next_open':
            return float(row['open'])
        elif model == 'next_vwap':
            return float((row['open'] + row['high'] + row['low'] + row['close']) / 4)
        elif model == 'next_close':
            return float(row['close'])
        else:
            return float(row['open'])

    def get_exec_price_by_date(self, stock_data: Dict[str, pd.DataFrame],
                               code: str, exec_date) -> Optional[float]:
        """
        通过日期获取成交价格（用于回测主循环）

        参数:
            stock_data: {code: DataFrame} 字典
            code:       股票代码
            exec_date:  执行日期（T+1 日）

        返回:
            成交价格
        """
        if code not in stock_data:
            return None
        df = stock_data[code]
        if exec_date not in df.index:
            return None

        row = df.loc[exec_date]

        if self.model == 'close':
            return float(row['close'])
        elif self.model == 'next_open':
            return float(row['open'])
        elif self.model == 'next_vwap':
            return float((row['open'] + row['high'] + row['low'] + row['close']) / 4)
        elif self.model == 'next_close':
            return float(row['close'])
        else:
            return float(row['open'])

    def check_capacity(self, df: pd.DataFrame, date_idx: int,
                       target_shares: int, price: float) -> int:
        """
        检查成交量容量约束，超出则缩减股数

        参数:
            df:            股票日线
            date_idx:      执行日索引
            target_shares: 目标股数
            price:         成交价格

        返回:
            调整后的可行股数
        """
        if date_idx >= len(df):
            return 0
        volume = df['volume'].iloc[date_idx]
        max_shares_by_vol = int(volume * self.participation_rate)
        return min(target_shares, max_shares_by_vol)


# ============================================================
# 一、回测核心类
# ============================================================

class BacktestEngine:
    """
    多资产向量化回测引擎

    使用方式：
      engine = BacktestEngine(config)
      engine.run(data_dict, index_df)
      engine.report()
    """

    def __init__(self, config: dict = None):
        """
        参数:
            config: 回测配置字典（见下面的默认值）
        """
        self.config = {
            # —— 资金 ——
            'initial_capital': 1_000_000,     # 初始资金 100万
            'max_positions': 5,                # 最大持仓数
            'max_positions_low': 3,            # 震荡市最大持仓数

            # —— 选股 ——
            'j_threshold': 20,                 # KDJ J值阈值（保留兼容）
            'j_60d_min_threshold': 15,         # 60日内J最低阈值（新核心过滤）
            'small_candle_threshold': 0.02,    # 买入当天涨跌幅上限（参考买点共性：±2%内小K线）
            'min_score': 45,                   # 最低综合得分

            # —— 风控 ——
            'risk_per_trade': 0.02,            # 单笔风险敞口 2%
            'atr_stop_multiplier': 2.0,        # ATR 硬止损倍数（2.5→2.0，更快止损）
            'portfolio_stop_loss': 0.20,       # 组合最大回撤熔断 (20%)
            'max_sector_exposure': 0.40,       # 单一行业最大权重 (40%)
            'adx_trend_threshold': 25,         # ADX 趋势阈值 20→25（更严格判断"趋势市"）

            # —— 移动止盈 (Trailing Stop) ——
            'trailing_stop_multiplier': 2.0,   # 移动止盈 ATR 距离
            'trailing_activation': 0.04,       # 浮盈 4% 激活移动止盈（3→4，让利润跑更远）
            'partial_profit_taking': True,     # 是否启用分批止盈
            'partial_profit_levels': [0.10, 0.20],   # 浮盈达 10%/20% 时分批止盈
            'partial_exit_ratios': [0.25, 0.25],      # 每个级别卖出 25%（30→25，留更多仓位）

            # —— 持仓时间管理 ——
            'max_hold_days': 15,               # 最大持仓天数（15天强制清仓释放资金）
            'profit_deadline_days': 5,         # 第 N 天检查是否盈利
            'profit_deadline_threshold': 0.0,  # 到期最低收益率（低于此值清仓）
            'bbi_profit_protect_pct': 0.05,    # 浮盈超过此值后启用 BBI 保护（8→5，更早保护）
            'bbi_profit_exit_ratio': 0.3,      # BBI 保护触发时卖出比例（70→30%，让利润继续跑）

            # —— 成交模型 ——
            'execution_model': 'next_open',    # 成交模型: 'close'|'next_open'|'next_vwap'|'next_close'
            'volume_participation_rate': 0.05, # 最大成交量参与率 5%

            # —— 交易成本 ——
            'commission': 0.00025,             # 佣金 万2.5
            'stamp_tax': 0.001,                # 印花税 千1（卖出）
            'slippage': 0.001,                 # 滑点 千1
            'min_commission': 5,               # 最低佣金 5元

            # —— 分批建仓/减仓 ——
            'batch_entry_enabled': True,       # 启用分批建仓
            'batch_entry_days': 2,             # 建仓分 N 天
            'batch_entry_ratios': [0.5, 0.5],  # 每天买入比例
            'batch_improvement_check': True,   # 后续批次检查：价格反向则跳过
            'batch_exit_enabled': True,        # 启用分批减仓
            'batch_exit_days': 2,              # 减仓分 N 天
            'batch_exit_ratios': [0.5, 0.5],   # 每天卖出比例

            # —— 择时权重 ——
            'score_weights': {
                'oversold': 0.14,              # 超卖深度（18→14，弹性因子覆盖部分逻辑）
                'volume_shrink': 0.13,         # 缩量得分（16→13，弹性看全程缩量更全面）
                'small_candle': 0.08,          # 小K线企稳（10→8，微调）
                'prior_surge': 0.12,           # 前期放量异动
                'reversal': 0.11,              # 反转确认（12→11，弹性与反转互补）
                'bbi_trend': 0.10,             # BBI趋势
                'ma60_proximity': 0.07,        # MA60贴近
                'hot_industry': 0.12,          # 行业热度+质量
                'amihud': 0.03,                # 流动性弹性
                'rebound_elasticity': 0.10,    # 🆕 反弹弹性（预测反弹幅度）
            },

            # —— 其他 ——
            'risk_free_rate': 0.03,            # 无风险利率（3%）
            'trading_days_per_year': 252,
            'adaptive_params_enabled': True,   # 启用市场自适应参数
        }
        if config:
            self.config.update(config)

        # 成交模拟器
        self.executor = ExecutionSimulator(
            model=self.config.get('execution_model', 'next_open'),
            participation_rate=self.config.get('volume_participation_rate', 0.05)
        )

        # 运行时状态
        self.results = None
        self.trades = []
        self.daily_stats = None

    # ============================================================
    # 1. 主回测循环
    # ============================================================

    def run(self, stock_data: Dict[str, pd.DataFrame],
            index_df: pd.DataFrame = None,
            fundamentals_by_date: Dict[str, pd.DataFrame] = None,
            stock_sectors: Dict[str, str] = None) -> pd.DataFrame:
        """
        执行回测

        参数:
            stock_data:   {stock_code: DataFrame(含因子列)}
            index_df:     上证指数日线（用于 BBI 择时）
            fundamentals_by_date: {date_str: DataFrame(基本面数据)}
            stock_sectors: {stock_code: sector_name}（行业分类）

        返回:
            daily_stats: 每日净值和持仓的 DataFrame

        回测流程：
          每天：
            1. 择时判断（指数 BBI + ADX 趋势强度）
            2. 遍历所有股票，打分
            3. 排名 → 选 top N
            4. 对已持仓检查止损/止盈
            5. 卖出触发条件的，买入新入选的
            6. 记录每日净值和持仓
        """
        print("=" * 60)
        print("回测开始")
        print("=" * 60)

        # —— 初始化 ——
        cfg = self.config
        config_saved_min = cfg['min_score']  # 保存用户配置的 min_score 作为下限
        cash = cfg['initial_capital']
        pending_cash = 0         # 卖出资金延迟一天到账（T日卖出 → T+1日可用）
        positions = {}           # {stock: {'shares': int, 'cost': float, 'buy_date': date}}
        yesterday_top3_codes = set()     # 三天候选池：昨天的Top3
        day_before_top3_codes = set()   # 三天候选池：前天的Top3
        portfolio_values = []
        daily_records = []
        self.trades = []

        # 获取交易日历 —— 优先用指数日期（最全），否则取所有股票日期的并集
        # 不能用交集：不同股票上市时间不同，交集会随股票数增加趋近于零
        if index_df is not None and not index_df.empty:
            trading_dates = sorted(index_df.index)
        else:
            all_dates = set()
            for df in stock_data.values():
                all_dates.update(df.index)
            trading_dates = sorted(all_dates)

        if len(trading_dates) < 60:
            print("[错误] 交易日不足60天，无法回测")
            return pd.DataFrame()

        print(f"  股票池: {len(stock_data)} 只")
        print(f"  交易日: {len(trading_dates)} 天")
        print(f"  日期范围: {trading_dates[0]} ~ {trading_dates[-1]}")

        # —— 主循环 ——
        warmup_days = 60  # 预热期（等因子计算有效）
        peak_value = cfg['initial_capital']
        circuit_breaker = False

        for i, date in enumerate(trading_dates):
            if i < warmup_days:
                portfolio_values.append(cfg['initial_capital'])
                continue

            # —— 前一天卖出的资金今日到账 ——
            cash += pending_cash
            pending_cash = 0

            date_str = date.strftime('%Y%m%d')

            # === 成交执行日期：信号在 T 日收盘后产生，执行在 T+1 日 ===
            exec_date = trading_dates[i + 1] if i + 1 < len(trading_dates) else date

            # ===== 第1步：择时判断 =====
            # 一级择时：指数 BBI 判断
            index_above_bbi = True  # 默认乐观
            if index_df is not None and date in index_df.index:
                idx_data = index_df.loc[:date].tail(30)
                if len(idx_data) >= 24:
                    from factor_engine import calc_bbi
                    idx_bbi = calc_bbi(idx_data)
                    if not np.isnan(idx_bbi[-1]):
                        index_above_bbi = idx_data['close'].iloc[-1] > idx_bbi[-1]

            # 二级择时：ADX 趋势强度
            market_adx = 30  # 默认趋势市
            market_in_trend = market_adx >= cfg['adx_trend_threshold']

            # 三级择时：全市场宽度（breadth timing）
            # 每20个交易日更新一次市场宽度（节省计算）
            breadth_info = None
            if i % 20 == 0 or i == warmup_days:
                try:
                    from data_engine import calc_market_breadth, breadth_to_position
                    breadth_raw = calc_market_breadth(stock_data, date)
                    breadth_info = breadth_to_position(breadth_raw)
                    # 缓存到 cfg 里供后续使用
                    cfg['_breadth_cache'] = breadth_info
                    if i % 120 == 0:
                        print(f"  [市场宽度] {date.strftime('%Y-%m-%d')} → "
                              f"MA20站上率:{breadth_raw['breadth_ma20']:.1%} | "
                              f"涨跌比:{breadth_raw['up_down_ratio']:.2f} | "
                              f"格局:{breadth_info['regime']}")
                except Exception:
                    pass
            # 使用缓存的宽度信息
            breadth_info = cfg.get('_breadth_cache', None)

            # 综合择时：三级信号取交集
            if index_above_bbi and market_in_trend:
                max_pos = cfg['max_positions']
                risk_per_trade = cfg['risk_per_trade']
            elif index_above_bbi or market_in_trend:
                max_pos = max(3, cfg['max_positions_low'])
                risk_per_trade = cfg['risk_per_trade'] * 0.7
            else:
                max_pos = cfg['max_positions_low']
                risk_per_trade = cfg['risk_per_trade'] * 0.5

            # —— 市场状态自适应参数（仅调技术指标，不调仓位）——
            cfg['_breadth_regime'] = 'neutral'  # 默认
            if cfg.get('adaptive_params_enabled', True) and index_df is not None:
                try:
                    from factor_engine import calc_market_regime, calc_adaptive_params
                    regime_info = calc_market_regime(index_df.loc[:date])
                    adaptive = calc_adaptive_params(regime_info, cfg)

                    # 自适应参数只覆盖技术类参数（J阈值、ATR、止盈等）
                    # 仓位和风险由市场宽度最终决定（不在此覆盖）
                    cfg['j_threshold'] = adaptive.get('j_threshold', cfg['j_threshold'])
                    cfg['atr_stop_multiplier'] = adaptive.get('atr_stop_multiplier', cfg['atr_stop_multiplier'])
                    cfg['trailing_stop_multiplier'] = adaptive.get('trailing_stop_multiplier', cfg['trailing_stop_multiplier'])
                    cfg['trailing_activation'] = adaptive.get('trailing_activation', cfg['trailing_activation'])
                    # min_score：取自适应值和配置值的较大者
                    adaptive_min = adaptive.get('min_score', cfg['min_score'])
                    cfg['min_score'] = max(adaptive_min, config_saved_min)

                    if i % 120 == 0:
                        print(f"  [市场状态] {date.strftime('%Y-%m-%d')} → "
                              f"{regime_info['regime']} | "
                              f"波动率分位:{regime_info['vol_percentile']:.0f}% | "
                              f"J阈值:{cfg['j_threshold']} | "
                              f"ATR止损×{cfg['atr_stop_multiplier']}")
                except Exception:
                    pass

            # ==== 市场宽度：最终仓位决策（硬上限，不可被覆盖）====
            if breadth_info is not None:
                b_regime = breadth_info.get('regime', 'neutral')
                if b_regime == 'bear':
                    max_pos = 0
                    risk_per_trade = 0
                elif b_regime == 'cautious':
                    max_pos = min(max_pos, breadth_info.get('max_positions', 3))
                    risk_per_trade = min(risk_per_trade, breadth_info.get('risk_per_trade', 0.012))
                cfg['_breadth_regime'] = b_regime

            # 季节性安全阀：春节后+五穷六绝 额外降仓
            if date.month in (3, 6) and max_pos > 0:
                max_pos = max(1, max_pos - 1)
                risk_per_trade = min(risk_per_trade, 0.010)

            # ===== 第2步：股票打分（向量化）=====
            # 从全市场截面一次性提取因子值 → DataFrame → 向量化过滤+打分+排名
            # 复杂度：O(N) 构建行 + O(1) 向量化计算，替代原 O(N) 逐股循环
            stock_rows = []
            for code, df in stock_data.items():
                if date not in df.index:
                    continue
                row = df.loc[date]
                # 只收集关键因子列，NaN 在向量化层统一处理
                stock_rows.append({
                    'code': code,
                    'close': row.get('close', np.nan),
                    'open': row.get('open', np.nan),
                    'high': row.get('high', np.nan),
                    'low': row.get('low', np.nan),
                    'kdj_j': row.get('kdj_j', np.nan),
                    'kdj_j_prev': row.get('kdj_j_prev', np.nan),
                    'kdj_j_60d_min': row.get('kdj_j_60d_min', np.nan),
                    'bbi': row.get('bbi', np.nan),
                    'ma5': row.get('ma5', np.nan),
                    'ma20': row.get('ma20', np.nan),
                    'ma60': row.get('ma60', np.nan),
                    'vol_pattern_valid': row.get('vol_pattern_valid', 0),
                    'vol_pattern_score': row.get('vol_pattern_score', 0),
                    'volume_ratio': row.get('volume_ratio', np.nan),
                    'volume_ma20': row.get('volume_ma20', np.nan),
                    'ret_20d': row.get('ret_20d', np.nan),
                    'atr14': row.get('atr14', np.nan),
                    'amihud': row.get('amihud', np.nan),
                    'turnover_change': row.get('turnover_change', np.nan),
                    'limit_down_dist': row.get('limit_down_dist', 0),
                    'had_volume_surge': row.get('had_volume_surge', 0),
                    'surge_type_big': row.get('surge_type_big', 0),
                    'surge_type_stack': row.get('surge_type_stack', 0),
                    'rebound_elasticity': row.get('rebound_elasticity', 50.0),
                    'daily_ret': row.get('daily_ret', np.nan),
                })

            if not stock_rows:
                # 当天没有股票数据，但需保留空 scores 让卖出逻辑正常运行
                scores = []
                target_stocks = {}
                yesterday_top3_codes = set()
                day_before_top3_codes = set()
            else:
                df_scores = pd.DataFrame(stock_rows)

                # ============ 新版选股逻辑（基于参考买点共性）============
                # 硬性过滤（四层漏斗）:
                #   L1: 长期趋势向上 — 价格>MA60 + BBI>MA60 (91%/82%覆盖)
                #   L2: 近期曾极度超卖 — J_60d_min < 15 (100%覆盖)
                #   L3: 前20天曾有放量异动 — had_volume_surge==1 (量随价升)
                #   L4: 买入当天小K线企稳 — |日涨跌幅| ≤ 2% (参考买点共性)
                #
                #  删除的旧过滤条件:
                #    ✗ price>MA5 (仅36%满足) → 改为加分项
                #    ✗ VolPattern==1 (仅18%满足) → 改为加分项
                #    ✗ J<j_threshold → 改为J_60d_min判断"曾极度超卖"

                j_min_threshold = cfg.get('j_60d_min_threshold', 15)
                candle_limit = cfg.get('small_candle_threshold', 0.02)

                mask = (
                    df_scores['kdj_j'].notna() &
                    df_scores['kdj_j_60d_min'].notna() &
                    df_scores['bbi'].notna() &
                    df_scores['ma60'].notna() &
                    df_scores['close'].notna() &
                    df_scores['daily_ret'].notna() &
                    (df_scores['kdj_j_60d_min'] < j_min_threshold) &     # L2: 60日内曾极度超卖
                    (df_scores['bbi'] > df_scores['ma60']) &             # L1: BBI > MA60
                    (df_scores['close'] > df_scores['ma60']) &           # L1: 长期趋势向上
                    (df_scores['daily_ret'].abs() <= candle_limit)       # L4: 当天小K线企稳（±2%内）
                )
                df_valid = df_scores.loc[mask].copy()

                if len(df_valid) == 0:
                    scores = []
                    target_stocks = {}
                    yesterday_top3_codes = set()
                    day_before_top3_codes = set()
                else:
                    # ---- 因子得分 (基于参考买点重新设计) ----

                    # ① 超卖深度得分：J_60d_min越低越好（-10比5更好）
                    df_valid['score_oversold'] = np.clip(
                        (j_min_threshold - df_valid['kdj_j_60d_min']) / j_min_threshold * 100, 0, 120
                    )

                    # ② 缩量得分：量比<0.8得高分，量比越低越好
                    df_valid['score_shrink'] = np.where(
                        df_valid['volume_ratio'].notna() & (df_valid['volume_ratio'] < 1.0),
                        np.clip((1.0 - df_valid['volume_ratio'].fillna(1)) / 0.5 * 100, 0, 100),
                        0
                    )

                    # ③ 小K线企稳得分：当日涨跌幅在±2%内得高分（买点当天多为小K线）
                    df_valid['daily_ret_abs'] = df_valid['daily_ret'].fillna(0).abs()
                    df_valid['score_small_candle'] = np.clip(
                        100 - df_valid['daily_ret_abs'] / 0.04 * 100, 0, 100
                    )

                    # ④ 放量异动得分：前25天爆量阳线 或 前20天连续堆量（参考买点共性 #2）
                    #    两种模式各占50分，叠加满分100
                    df_valid['score_surge'] = (
                        df_valid['had_volume_surge'].fillna(0) * 70 +
                        df_valid.get('surge_type_big', pd.Series(0, index=df_valid.index)).fillna(0) * 15 +
                        df_valid.get('surge_type_stack', pd.Series(0, index=df_valid.index)).fillna(0) * 15
                    ).clip(0, 100)

                    # ⑤ 反转确认得分（参考买点共性）
                    #    - J拐头向上(30分)
                    #    - 缩量(40分)：量比<0.8
                    #    - 小K线(30分)：|日涨幅|<2%
                    j_turning = (
                        df_valid['kdj_j_prev'].notna() &
                        (df_valid['kdj_j'] > df_valid['kdj_j_prev'])
                    ).astype(float)
                    still_shrinking = (
                        df_valid['volume_ratio'].notna() &
                        (df_valid['volume_ratio'] < 0.8)
                    ).astype(float)
                    small_candle = (df_valid['daily_ret_abs'] < 0.02).astype(float)
                    df_valid['score_reversal'] = np.clip(
                        j_turning * 30 + still_shrinking * 40 + small_candle * 30, 0, 100
                    )

                    # ⑥ BBI趋势得分
                    df_valid['score_bbi'] = np.minimum(
                        100, (df_valid['bbi'] - df_valid['ma60']) / df_valid['ma60'] * 500
                    )

                    # ⑦ 价格贴近MA60得分（离MA60越近=回调到位）
                    ma60_deviation = np.abs(df_valid['close'] - df_valid['ma60']) / df_valid['ma60']
                    df_valid['score_ma60'] = np.clip(100 - ma60_deviation * 500, 0, 100)

                    # ⑧ 行业热度+质量（合并因子：行业动量 65% + 基本面质量 35%）
                    if stock_sectors and len(stock_sectors) > 0:
                        df_valid['sector'] = df_valid['code'].map(stock_sectors)
                        sector_ret = df_valid.groupby('sector')['ret_20d'].mean()
                        sector_rank = sector_ret.rank(pct=True)
                        sector_score = 30 + sector_rank * 70
                        df_valid['score_hot_raw'] = df_valid['sector'].map(sector_score).fillna(50)
                    else:
                        ret20 = df_valid['ret_20d'].fillna(0)
                        df_valid['score_hot_raw'] = np.clip(
                            ret20.rank(pct=True) * 100, 0, 100
                        )

                    # 质量得分（合并进行业热度）
                    df_valid['score_quality'] = 50.0
                    if fundamentals_by_date and date_str in fundamentals_by_date:
                        funda = fundamentals_by_date[date_str]
                        if not funda.empty and 'ts_code' in funda.columns:
                            funda_sub = funda[['ts_code', 'pe', 'roe']].copy()
                            funda_sub = funda_sub[funda_sub['ts_code'].isin(df_valid['code'])]
                            if not funda_sub.empty:
                                df_valid = df_valid.merge(
                                    funda_sub, left_on='code', right_on='ts_code', how='left'
                                )
                                pe_adj = np.where(
                                    df_valid['pe'].notna() & (df_valid['pe'] > 0),
                                    np.clip((20 - df_valid['pe']) / 20 * 25, -25, 25), 0
                                )
                                roe_adj = np.where(
                                    df_valid['roe'].notna() & (df_valid['roe'] > 0),
                                    np.clip((df_valid['roe'] - 5) / 15 * 25, -25, 25), 0
                                )
                                df_valid['score_quality'] = np.clip(50 + pe_adj + roe_adj, 0, 100)

                    # 行业热度 = 行业动量(65%) + 基本面质量(35%)
                    df_valid['score_hot'] = (
                        df_valid['score_hot_raw'] * 0.65 + df_valid['score_quality'] * 0.35
                    )
                    if df_valid['amihud'].notna().any():
                        amihud_rank = df_valid['amihud'].rank(pct=True)
                        df_valid['score_amihud'] = np.clip(
                            100 - np.abs(amihud_rank - 0.5) * 200, 0, 100
                        )
                    else:
                        df_valid['score_amihud'] = 50.0

                    # ⑩ 反弹弹性得分：直接使用因子值（0-100，50为中性）
                    df_valid['score_elasticity'] = df_valid['rebound_elasticity'].fillna(50).clip(0, 100)

                    # ---- 综合得分 ----
                    w = cfg['score_weights']
                    df_valid['score'] = (
                        df_valid['score_oversold'] * w.get('oversold', 0.14) +
                        df_valid['score_shrink'] * w.get('volume_shrink', 0.13) +
                        df_valid['score_small_candle'] * w.get('small_candle', 0.08) +
                        df_valid['score_surge'] * w.get('prior_surge', 0.12) +
                        df_valid['score_reversal'] * w.get('reversal', 0.11) +
                        df_valid['score_bbi'] * w.get('bbi_trend', 0.10) +
                        df_valid['score_ma60'] * w.get('ma60_proximity', 0.07) +
                        df_valid['score_hot'] * w.get('hot_industry', 0.12) +
                        df_valid['score_amihud'] * w.get('amihud', 0.03) +
                        df_valid['score_elasticity'] * w.get('rebound_elasticity', 0.10)
                    )

                    # ==== 行业中性化 ====
                    if stock_sectors and len(stock_sectors) > 0:
                        df_valid['sector'] = df_valid['code'].map(stock_sectors)
                        sector_stats = df_valid.groupby('sector')['score'].agg(['mean', 'std'])
                        df_valid['sector_mean'] = df_valid['sector'].map(sector_stats['mean'])
                        df_valid['sector_std'] = df_valid['sector'].map(sector_stats['std']).fillna(1)
                        df_valid['score_z'] = np.where(
                            df_valid['sector_std'] > 0,
                            (df_valid['score'] - df_valid['sector_mean']) / df_valid['sector_std'], 0
                        )
                        df_valid['score_final'] = df_valid['score'] + df_valid['score_z'].clip(-2, 3) * 2
                    else:
                        df_valid['score_final'] = df_valid['score']

                    # 最低得分过滤 + 排名
                    df_valid = df_valid[df_valid['score_final'] >= cfg['min_score']]
                    df_valid = df_valid.sort_values('score_final', ascending=False)

                    # 每行业最多3只
                    if stock_sectors and len(stock_sectors) > 0 and 'sector' in df_valid.columns:
                        sector_counts = {}
                        filtered_rows = []
                        for _, r in df_valid.iterrows():
                            sec = r.get('sector', 'unknown')
                            cnt = sector_counts.get(sec, 0)
                            if cnt < 3:
                                filtered_rows.append(r)
                                sector_counts[sec] = cnt + 1
                        df_valid = pd.DataFrame(filtered_rows)

                    # 转为list-of-dict
                    scores = []
                    for _, r in df_valid.iterrows():
                        scores.append({
                            'code': r['code'],
                            'score': r.get('score_final', r['score']),
                            'score_oversold': r['score_oversold'],
                            'score_shrink': r['score_shrink'],
                            'score_small_candle': r['score_small_candle'],
                            'score_surge': r['score_surge'],
                            'score_reversal': r['score_reversal'],
                            'score_bbi': r['score_bbi'],
                            'j_val': r['kdj_j'],
                            'price': r['close'],
                            'atr': r['atr14'],
                            'elasticity': r.get('rebound_elasticity', 50),
                        })
                    # —— 三天候选池：今天Top3 + 昨天Top3 + 前天Top3，选最高分 ——
                    today_top3 = {s['code'] for s in scores[:3]}
                    combined_codes = today_top3 | yesterday_top3_codes | day_before_top3_codes
                    combined = [s for s in scores if s['code'] in combined_codes]
                    combined.sort(key=lambda x: x['score'], reverse=True)
                    target_stocks = {s['code']: s for s in combined[:max_pos]}
                    # 滚动：前天的退场，昨天的变前天，今天的变昨天
                    day_before_top3_codes = yesterday_top3_codes
                    yesterday_top3_codes = today_top3

            # ===== 第3步：更新持仓状态 + 检查卖出信号 =====
            stocks_to_sell = []
            for code, pos in list(positions.items()):
                if code not in stock_data:
                    continue
                df = stock_data[code]
                if date not in df.index:
                    continue

                row = df.loc[date]
                current_price = row['close']

                # —— 更新持仓的最高价记录（用于移动止盈） ——
                if 'highest_since_entry' not in pos:
                    pos['highest_since_entry'] = pos['cost']
                pos['highest_since_entry'] = max(pos['highest_since_entry'], current_price)

                # —— 计算持仓天数 ——
                buy_date = pos['buy_date']
                trading_dates_subset = [d for d in trading_dates if d >= buy_date and d <= date]
                days_held = len(trading_dates_subset)
                pos['days_held'] = days_held

                # —— 多层卖出信号检测 ——
                should_sell, reason, exit_ratio = self._check_sell_signals(
                    row=row,
                    pos=pos,
                    current_price=current_price,
                    df=df,
                    date=date,
                    code=code,
                    target_stocks=target_stocks,
                    trading_dates=trading_dates,
                )

                if should_sell:
                    # 计算实际卖出的股数（支持分批止盈）
                    sell_shares = int(pos['shares'] * exit_ratio)
                    if sell_shares <= 0:
                        sell_shares = pos['shares']
                    stocks_to_sell.append((code, current_price, reason, sell_shares))

            # ===== 第4步：执行卖出 =====
            for code, signal_price, reason, sell_shares in stocks_to_sell:
                if code not in positions:
                    continue
                pos = positions[code]

                # 获取真实成交价（T+1 开盘价，消除前视偏差）
                exec_price = self.executor.get_exec_price_by_date(
                    stock_data, code, exec_date
                )
                if exec_price is None or exec_price <= 0:
                    exec_price = signal_price  # fallback: T日收盘价

                # 实际卖出股数（不超过当前持仓，支持分批止盈）
                actual_sell = min(sell_shares, pos['shares'])
                if actual_sell <= 0:
                    continue

                sell_amount = actual_sell * exec_price
                commission = max(cfg['min_commission'], sell_amount * cfg['commission'])
                stamp_tax = sell_amount * cfg['stamp_tax']
                slippage = sell_amount * cfg['slippage']
                # 卖出资金延迟一天到账（T日卖出 → T+1日才能用于买入）
                pending_cash += sell_amount - commission - stamp_tax - slippage

                # 记录交易
                pnl = (exec_price - pos['cost']) * actual_sell - commission - stamp_tax - slippage
                pnl_pct = (exec_price - pos['cost']) / pos['cost'] * 100
                self.trades.append({
                    'code': code,
                    'buy_date': pos['buy_date'],
                    'sell_date': exec_date,
                    'buy_price': pos['cost'],
                    'sell_price': exec_price,
                    'shares': actual_sell,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'reason': reason,
                })

                # 部分卖出：减仓保留；全部卖出：删除
                if actual_sell >= pos['shares']:
                    del positions[code]
                else:
                    pos['shares'] -= actual_sell

            # 熔断检查
            current_equity = cash + pending_cash + sum(
                positions[c]['shares'] * stock_data[c].loc[date]['close']
                for c in positions if date in stock_data[c].index
            )
            if current_equity < peak_value * (1 - cfg['portfolio_stop_loss']):
                circuit_breaker = True
                # 清仓（使用真实成交价）
                for code in list(positions.keys()):
                    if code in stock_data:
                        exec_price_cb = self.executor.get_exec_price_by_date(
                            stock_data, code, exec_date
                        )
                        if exec_price_cb is None or exec_price_cb <= 0:
                            exec_price_cb = stock_data[code].loc[date]['close']
                    else:
                        exec_price_cb = pos['cost']
                    pos = positions[code]
                    sell_amount = pos['shares'] * exec_price_cb
                    commission = max(cfg['min_commission'], sell_amount * cfg['commission'])
                    stamp_tax = sell_amount * cfg['stamp_tax']
                    pending_cash += sell_amount - commission - stamp_tax
                    pnl = (exec_price_cb - pos['cost']) * pos['shares'] - commission - stamp_tax
                    self.trades.append({
                        'code': code,
                        'buy_date': pos['buy_date'],
                        'sell_date': exec_date,
                        'buy_price': pos['cost'],
                        'sell_price': exec_price_cb,
                        'shares': pos['shares'],
                        'pnl': pnl,
                        'pnl_pct': (exec_price_cb - pos['cost']) / pos['cost'] * 100,
                        'reason': '组合熔断',
                    })
                    del positions[code]
                print(f"  [熔断] {date.strftime('%Y-%m-%d')} 触发组合熔断!")
                break

            # ===== 第4.5步：处理分批建仓（给仍在 building 阶段的仓位追加批次）=====
            if not circuit_breaker:
                cash = self._process_batch_entries(
                    positions, stock_data, exec_date, cash, cfg
                )

            # ===== 第4.8步：动态换仓 —— 高分新票替换亏损/横盘旧票 =====
            market_is_bull = cfg.get('_breadth_regime', 'neutral') == 'bull'
            cfg['_market_is_bull'] = market_is_bull   # 传给卖出信号用
            if not circuit_breaker and len(positions) > 0 and len(scores) > 0:
                top_score = scores[0]['score'] if scores else 0
                for code, pos in list(positions.items()):
                    if code not in stock_data or date not in stock_data[code].index:
                        continue
                    current_price = stock_data[code].loc[date]['close']
                    floating_pnl = (current_price - pos['cost']) / pos['cost']
                    entry_score = pos.get('entry_score', 0)
                    # 条件：亏损或横盘（浮盈<1%）+ 有高分候选（比入场分高15+）
                    if (floating_pnl < 0.01
                            and top_score > entry_score + 15
                            and top_score >= 60):
                        # 卖出旧票（按T+1开盘价成交）
                        exec_price_swap = self.executor.get_exec_price_by_date(
                            stock_data, code, exec_date
                        )
                        if exec_price_swap is None or exec_price_swap <= 0:
                            exec_price_swap = current_price
                        sell_amount = pos['shares'] * exec_price_swap
                        commission = max(cfg['min_commission'], sell_amount * cfg['commission'])
                        stamp_tax = sell_amount * cfg['stamp_tax']
                        slippage = sell_amount * cfg['slippage']
                        pending_cash += sell_amount - commission - stamp_tax - slippage
                        pnl = (exec_price_swap - pos['cost']) * pos['shares'] - commission - stamp_tax - slippage
                        self.trades.append({
                            'code': code, 'buy_date': pos['buy_date'],
                            'sell_date': exec_date, 'buy_price': pos['cost'],
                            'sell_price': exec_price_swap, 'shares': pos['shares'],
                            'pnl': pnl, 'pnl_pct': (exec_price_swap - pos['cost']) / pos['cost'] * 100,
                            'reason': '换仓(高分替换)',
                        })
                        del positions[code]

            # ===== 第5步：执行买入 =====
            if not circuit_breaker:
                available_slots = max_pos - len(positions)
                if available_slots > 0:
                    for s in scores:
                        code = s['code']
                        if code in positions:
                            continue
                        if available_slots <= 0:
                            break

                        stock_score = s['score']

                        exec_price = self.executor.get_exec_price_by_date(
                            stock_data, code, exec_date
                        )
                        if exec_price is None or exec_price <= 0:
                            continue

                        # ==== 涨跌停 + 跳空保护 ====
                        from data_engine import is_price_limit_day
                        if code in stock_data and date in stock_data[code].index:
                            stock_df = stock_data[code]
                            date_idx = stock_df.index.get_loc(date)
                            t_limit_up, t_limit_down = is_price_limit_day(stock_df, date_idx, code)
                            if t_limit_up or t_limit_down:
                                continue
                            if exec_date in stock_df.index:
                                t1_idx = stock_df.index.get_loc(exec_date)
                                t1_up, t1_down = is_price_limit_day(stock_df, t1_idx, code)
                                if t1_up or t1_down:
                                    continue

                        signal_price = s['price']
                        if exec_price > signal_price * 1.035:
                            continue
                        if exec_price < signal_price * 0.95:
                            continue

                        atr = s['atr']
                        if pd.isna(atr) or atr <= 0:
                            continue

                        from factor_engine import calc_position_size
                        per_slot_cash = cash / (available_slots + len(positions) + 1)
                        shares = calc_position_size(per_slot_cash, risk_per_trade, atr, exec_price)
                        if shares <= 0:
                            continue

                        buy_amount = shares * exec_price
                        commission = max(cfg['min_commission'], buy_amount * cfg['commission'])
                        slippage = buy_amount * cfg['slippage']
                        total_cost = buy_amount + commission + slippage

                        # ==== 单只仓位上限：得分 + 市场 + 弹性 三重动态 ====
                        elasticity = s.get('elasticity', 50)  # 反弹弹性分
                        if market_is_bull and stock_score >= 80:
                            single_cap = 0.50
                        elif market_is_bull:
                            single_cap = 0.35
                        else:
                            single_cap = 0.25
                        # 弹性修正：高弹性放大，低弹性收缩
                        if elasticity >= 70:
                            single_cap *= 1.3   # 高弹性 → 敢重仓
                        elif elasticity < 40:
                            single_cap *= 0.7   # 低弹性 → 谨慎

                        if total_cost > cash * single_cap:
                            buy_amount = cash * single_cap - commission - slippage
                            shares = int(buy_amount / exec_price / 100) * 100
                            if shares <= 0:
                                continue
                            buy_amount = shares * exec_price
                            commission = max(cfg['min_commission'], buy_amount * cfg['commission'])
                            slippage = buy_amount * cfg['slippage']
                            total_cost = buy_amount + commission + slippage

                        if total_cost <= cash:
                            # ==== 动态分批：高分重仓，低分谨慎 ====
                            if cfg.get('batch_entry_enabled', False) and cfg.get('batch_entry_days', 1) > 1:
                                if stock_score >= 80:
                                    batch_ratios = [1.0]            # 高分直接满仓
                                elif stock_score >= 60:
                                    batch_ratios = [0.7, 0.3]      # 中分第一批70%
                                else:
                                    batch_ratios = [0.5, 0.5]      # 低分分批各50%

                                batch1 = batch_ratios[0]
                                batch1_shares = int(shares * batch1 / 100) * 100
                                if batch1_shares < 100:
                                    batch1_shares = shares
                                batch1_amount = batch1_shares * exec_price
                                batch1_comm = max(cfg['min_commission'], batch1_amount * cfg['commission'])
                                batch1_slip = batch1_amount * cfg['slippage']
                                batch1_total = batch1_amount + batch1_comm + batch1_slip

                                if batch1_total <= cash:
                                    cash -= batch1_total
                                    positions[code] = {
                                        'shares': batch1_shares,
                                        'cost': exec_price,
                                        'buy_date': exec_date,
                                        'target_shares': shares,
                                        'entry_phase': 'building',
                                        'batch_num': 1,
                                        'batch_ratios': batch_ratios,
                                        'signal_atr': atr,
                                        'entry_score': stock_score,
                                    }
                                    available_slots -= 1
                            else:
                                cash -= total_cost
                                positions[code] = {
                                    'shares': shares,
                                    'cost': exec_price,
                                    'buy_date': exec_date,
                                    'entry_phase': 'complete',
                                    'entry_score': stock_score,
                                }
                                available_slots -= 1

            # ===== 第6步：记录每日净值 =====
            equity = cash + pending_cash  # pending_cash 也是资产的一部分（T+1到账）
            for code, pos in positions.items():
                if code in stock_data and date in stock_data[code].index:
                    equity += pos['shares'] * stock_data[code].loc[date]['close']
                else:
                    equity += pos['shares'] * pos['cost']

            peak_value = max(peak_value, equity)
            drawdown = (equity - peak_value) / peak_value

            daily_records.append({
                'date': date,
                'equity': equity,
                'cash': cash,
                'positions': len(positions),
                'drawdown': drawdown,
            })

            if i % 60 == 0:
                print(f"  [{date.strftime('%Y-%m-%d')}] 净值: {equity:,.0f}  |  "
                      f"持仓: {len(positions)}只 | 现金: {cash:,.0f} | 回撤: {drawdown:.2%}")

        # —— 整理结果 ——
        self.daily_stats = pd.DataFrame(daily_records).set_index('date')
        if len(self.daily_stats) > 0:
            self.daily_stats['returns'] = self.daily_stats['equity'].pct_change()

        print(f"\n回测完成: {len(self.trades)} 笔交易, ")
        print(f"  最终净值: {self.daily_stats['equity'].iloc[-1]:,.0f}" if len(self.daily_stats) > 0 else "  无数据")

        return self.daily_stats

    # ============================================================
    # 1.5 多层卖出信号检测
    # ============================================================

    def _check_sell_signals(self, row, pos: dict, current_price: float,
                            df: pd.DataFrame, date, code: str,
                            target_stocks: dict, trading_dates: list
                            ) -> Tuple[bool, str, float]:
        """
        多层卖出信号检测（按优先级从高到低）

        层级设计逻辑：
          Layer 1 (灾难止损): 固定 ATR 硬止损，从成本价计算，永不调整
          Layer 2 (移动止盈): 浮盈激活后，止损价跟随最高价上移，锁定利润
          Layer 3 (BBI 趋势保护): 浮盈后跌破 BBI = 趋势可能反转，部分减仓
          Layer 4 (时间止损): 持仓超时或未达盈利预期
          Layer 5 (质量止损): 异常放量阴线
          Layer 6 (排名淘汰): 不在当天目标池

        返回:
            (should_sell, reason, exit_ratio)
            - should_sell: 是否触发卖出
            - reason:      卖出原因
            - exit_ratio:  卖出比例 (0.0~1.0)，1.0=全部卖出

        面试考点：
          Q: 为什么移动止盈要设置激活阈值（3%）？
          A: 如果浮盈 0% 就激活，止损会立刻收紧，容易被正常波动震出。
             3% 的激活阈值给股票一个"自由呼吸"的空间，
             相当于"先让我赚一点，再开始保护利润"。
        """
        cfg = self.config
        avg_cost = pos['cost']
        highest = pos.get('highest_since_entry', avg_cost)
        atr_val = row.get('atr14', np.nan)
        floating_pnl = (current_price - avg_cost) / avg_cost
        days_held = pos.get('days_held', 0)

        # ================================================================
        # Layer 1: 硬止损 — ATR 从成本价计算，永不调整（灾难防护）
        # ================================================================
        if not pd.isna(atr_val) and atr_val > 0:
            hard_stop = avg_cost - cfg['atr_stop_multiplier'] * atr_val
            if current_price < hard_stop:
                return True, f'硬止损(ATR={atr_val:.2f})', 1.0

        # ================================================================
        # Layer 2: 移动止盈 — 浮盈激活后，止损跟随最高价上移
        # ================================================================
        if not pd.isna(atr_val) and atr_val > 0 and floating_pnl > cfg['trailing_activation']:
            # 止损价 = 持仓期间最高价 - N倍ATR（只升不降）
            trailing_stop = highest - cfg['trailing_stop_multiplier'] * atr_val

            if current_price < trailing_stop:
                return True, f'移动止盈(锁定{floating_pnl:.1%})', 1.0

            # —— 分批止盈：浮盈达到指定水平时部分卖出 ——
            if cfg.get('partial_profit_taking', True):
                levels = cfg.get('partial_profit_levels', [0.10, 0.20])
                ratios = cfg.get('partial_exit_ratios', [0.3, 0.3])

                # 检查是否到达止盈级别（使用当前浮盈 vs 最高浮盈判断是否首次触发）
                for level, ratio in zip(levels, ratios):
                    if floating_pnl >= level:
                        # 检查是否已经在这个级别止盈过
                        level_key = f'profit_taken_{level}'
                        if not pos.get(level_key, False):
                            pos[level_key] = True
                            return True, f'分批止盈(浮盈{floating_pnl:.1%}达{level:.0%})', ratio

        # ================================================================
        # Layer 3: BBI 趋势保护 — 浮盈后跌破 BBI 部分减仓
        # ================================================================
        if floating_pnl > cfg.get('bbi_profit_protect_pct', 0.08):
            bbi_val = row.get('bbi')
            if not pd.isna(bbi_val) and current_price < bbi_val:
                exit_ratio = cfg.get('bbi_profit_exit_ratio', 0.7)
                return True, f'浮盈{floating_pnl:.1%}后跌破BBI', exit_ratio

        # ================================================================
        # Layer 4: 动态时间止损 — 牛市更紧，熊市更松
        # ================================================================
        market_is_bull = cfg.get('_market_is_bull', False)

        if market_is_bull:
            # 牛市：机会成本高，亏损票快速换仓
            if days_held >= 7 and floating_pnl < -0.02:
                return True, f'牛市第{days_held}天亏损({floating_pnl:.1%})', 1.0
            if days_held >= 10 and floating_pnl < 0:
                return True, f'牛市第{days_held}天不赚({floating_pnl:.1%})', 1.0
            if days_held >= 12 and floating_pnl < 0.02:
                return True, f'牛市第{days_held}天微利({floating_pnl:.1%})', 1.0

        # 通用时间止损（牛市/熊市都适用）
        # 4a. 第15天亏损且幅度>3% → 砍
        if days_held >= 15 and floating_pnl < -0.03:
            return True, f'第{days_held}天深度亏损({floating_pnl:.1%})', 1.0

        # 4b. 第18天亏损 → 砍
        if days_held >= 18 and floating_pnl < 0:
            return True, f'第{days_held}天亏损({floating_pnl:.1%})', 1.0

        # 4c. 第21天微利 → 砍（超跌反弹的合理时间窗口）
        if days_held >= 21 and floating_pnl < 0.02:
            return True, f'第{days_held}天微利({floating_pnl:.1%})', 1.0

        # 4d. 第25天：任何亏损都砍
        if days_held >= 25 and floating_pnl < 0:
            return True, f'第{days_held}天亏损({floating_pnl:.1%})', 1.0

        # 4e. 第30天：终极上限全清
        if days_held >= 30:
            return True, f'持仓超30天({floating_pnl:.1%})', 1.0

        # ================================================================
        # Layer 5: 质量止损 — 放量阴线
        # 放量 = 成交量 > 20日均量 × 1.5，阴线 = 当日下跌
        # ================================================================
        if len(df.loc[:date]) >= 2:
            prev = df.loc[:date].iloc[-2]
            ret = (current_price - prev['close']) / prev['close']
            vol_ma20 = row.get('volume_ma20', 0)
            if ret < 0 and vol_ma20 > 0 and row['volume'] > vol_ma20 * 1.5:
                return True, '放量阴线', 1.0

        # ================================================================
        # 注意：不再因"不在当天排名内"而卖出
        # 只要没有触发以上卖出信号，就继续持有，让 Alpha 有充分时间兑现
        # ================================================================

        return False, '', 0.0

    # ============================================================
    # 1.6 分批建仓处理
    # ============================================================

    def _process_batch_entries(self, positions: dict, stock_data: dict,
                               exec_date, cash: float, cfg: dict) -> float:
        """
        处理仍在 building 阶段的仓位，追加后续批次

        逻辑：
          1. 遍历所有处于 building 状态的持仓
          2. 检查是否满足后续批次条件（改进检查）
          3. 以 exec_date 的价格买入下一批次
          4. 更新持仓成本和状态

        改进检查：如果 T+1 价格比第一批成本高太多（gap up），
        说明错过了最佳买点，跳过后续批次。
        """
        for code, pos in list(positions.items()):
            if pos.get('entry_phase') != 'building':
                continue
            if code not in stock_data:
                continue

            # 获取今日成交价
            exec_price = self.executor.get_exec_price_by_date(
                stock_data, code, exec_date
            )
            if exec_price is None or exec_price <= 0:
                continue

            target_shares = pos.get('target_shares', pos['shares'])
            current_shares = pos['shares']
            remaining = target_shares - current_shares
            if remaining < 100:
                # 太少，直接标记完成
                pos['entry_phase'] = 'complete'
                continue

            batch_num = pos.get('batch_num', 1)
            total_batches = cfg.get('batch_entry_days', 2)
            if batch_num >= total_batches:
                pos['entry_phase'] = 'complete'
                continue

            # —— 改进检查：后续批次价格不能比第一批差太多 ——
            if cfg.get('batch_improvement_check', True):
                first_cost = pos['cost']
                price_move = (exec_price - first_cost) / first_cost
                # 如果价格涨超 3%，跳过后续批次（买贵了）
                if price_move > 0.03:
                    pos['entry_phase'] = 'complete'
                    continue

            # 计算本批次股数（优先用持仓自己的batch_ratios，否则全局默认）
            batch_ratios = pos.get('batch_ratios', cfg.get('batch_entry_ratios', [0.5, 0.5]))
            remaining_ratio = sum(batch_ratios[batch_num:])
            if remaining_ratio <= 0:
                pos['entry_phase'] = 'complete'
                continue

            this_ratio = batch_ratios[batch_num] / remaining_ratio
            batch_shares = int(remaining * this_ratio / 100) * 100
            if batch_shares < 100:
                batch_shares = remaining

            batch_amount = batch_shares * exec_price
            commission = max(cfg['min_commission'], batch_amount * cfg['commission'])
            slippage = batch_amount * cfg['slippage']
            total_cost = batch_amount + commission + slippage

            if total_cost > cash * 0.25:
                batch_shares = int((cash * 0.25 - commission - slippage) / exec_price / 100) * 100
                if batch_shares < 100:
                    pos['entry_phase'] = 'complete'
                    continue
                batch_amount = batch_shares * exec_price
                commission = max(cfg['min_commission'], batch_amount * cfg['commission'])
                slippage = batch_amount * cfg['slippage']
                total_cost = batch_amount + commission + slippage

            if total_cost <= cash:
                cash -= total_cost
                # 更新加权平均成本
                total_value = (pos['cost'] * pos['shares'] + exec_price * batch_shares)
                pos['shares'] += batch_shares
                pos['cost'] = total_value / pos['shares']
                pos['batch_num'] = batch_num + 1
                pos['highest_since_entry'] = max(
                    pos.get('highest_since_entry', pos['cost']), exec_price
                )

                if pos['batch_num'] >= total_batches or pos['shares'] >= target_shares:
                    pos['entry_phase'] = 'complete'

        return cash

    # ============================================================
    # 2. 绩效分析
    # ============================================================

    def analyze(self) -> dict:
        """
        计算全套绩效指标

        面试时必须能默写出这些公式：
          Sharpe = (CAGR - rf) / σ_annual
          Sortino = (CAGR - rf) / σ_downside
          Calmar = CAGR / |MaxDD|
          MAR = CAGR / |MaxDD|  (同 Calmar，不同行业叫法不同)
          Profit Factor = 总盈利 / 总亏损
          Win Rate = 盈利笔数 / 总笔数
        """
        if self.daily_stats is None or len(self.daily_stats) == 0:
            return {'error': '无回测数据'}

        cfg = self.config
        stats = self.daily_stats
        returns = stats['returns'].dropna()

        if len(returns) == 0:
            return {'error': '无有效收益率数据'}

        n_years = len(returns) / cfg['trading_days_per_year']

        # —— 收益指标 ——
        total_return = stats['equity'].iloc[-1] / cfg['initial_capital'] - 1
        cagr = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0

        # —— 风险指标 ——
        annual_vol = returns.std() * np.sqrt(cfg['trading_days_per_year'])

        # 下行波动率（只算亏损天数的波动）
        downside_returns = returns[returns < 0]
        downside_vol = downside_returns.std() * np.sqrt(cfg['trading_days_per_year']) \
            if len(downside_returns) > 0 else annual_vol

        # 最大回撤
        cum = stats['equity'] / cfg['initial_capital']
        running_max = cum.expanding().max()
        drawdown_series = (cum - running_max) / running_max
        max_dd = drawdown_series.min()

        # —— 风险调整收益 ——
        rf = cfg['risk_free_rate']
        sharpe = (cagr - rf) / annual_vol if annual_vol > 0 else 0
        sortino = (cagr - rf) / downside_vol if downside_vol > 0 else 0
        calmar = cagr / abs(max_dd) if max_dd != 0 else 0

        # —— 交易指标 ——
        trades_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()

        if not trades_df.empty:
            win_trades = trades_df[trades_df['pnl'] > 0]
            lose_trades = trades_df[trades_df['pnl'] < 0]

            total_wins = win_trades['pnl'].sum() if len(win_trades) > 0 else 0
            total_losses = abs(lose_trades['pnl'].sum()) if len(lose_trades) > 0 else 0
            profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

            win_rate = len(win_trades) / len(trades_df) if len(trades_df) > 0 else 0

            avg_win = win_trades['pnl'].mean() if len(win_trades) > 0 else 0
            avg_loss = lose_trades['pnl'].mean() if len(lose_trades) > 0 else 0
            avg_pnl = trades_df['pnl'].mean()

            # 平均持有天数
            avg_hold_days = (trades_df['sell_date'] - trades_df['buy_date']).dt.days.mean()

            # 盈亏比
            payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        else:
            profit_factor = 0
            win_rate = 0
            avg_win = 0
            avg_loss = 0
            avg_pnl = 0
            avg_hold_days = 0
            payoff_ratio = 0

        # —— 换手率 ——
        daily_turnover = 0
        if not trades_df.empty:
            total_buy_amount = 0
            for _, t in trades_df.iterrows():
                total_buy_amount += t['buy_price'] * t['shares']
            avg_equity = stats['equity'].mean()
            daily_turnover = total_buy_amount / (avg_equity * n_years) if n_years > 0 else 0

        return {
            # 收益
            '总收益率': f"{total_return:.2%}",
            '年化收益(CAGR)': f"{cagr:.2%}",
            '累计净值': f"{stats['equity'].iloc[-1] / cfg['initial_capital']:.4f}",

            # 风险
            '年化波动率': f"{annual_vol:.2%}",
            '下行波动率': f"{downside_vol:.2%}",
            '最大回撤': f"{max_dd:.2%}",
            '最大回撤持续天数': self._calc_max_dd_duration(),

            # 风险调整
            '夏普比率(Sharpe)': f"{sharpe:.3f}",
            '索提诺比率(Sortino)': f"{sortino:.3f}",
            '卡尔玛比率(Calmar)': f"{calmar:.3f}",

            # 交易
            '总交易次数': len(self.trades),
            '胜率': f"{win_rate:.2%}",
            '盈亏比': f"{payoff_ratio:.2f}",
            '盈利因子(Profit Factor)': f"{profit_factor:.3f}",
            '平均单笔盈亏': f"{avg_pnl:,.0f}",
            '平均盈利': f"{avg_win:,.0f}",
            '平均亏损': f"{avg_loss:,.0f}",
            '平均持仓天数': f"{avg_hold_days:.1f}",
            '年化换手率': f"{daily_turnover:.2f}",

            # 其他
            '回测天数': len(returns),
            '回测年数': f"{n_years:.1f}",
        }

    def _calc_max_dd_duration(self) -> str:
        """计算最大回撤持续天数"""
        if self.daily_stats is None:
            return "N/A"
        cum = self.daily_stats['equity'] / self.config['initial_capital']
        running_max = cum.expanding().max()
        is_dd = cum < running_max

        max_duration = 0
        current_duration = 0
        for v in is_dd:
            if v:
                current_duration += 1
                max_duration = max(max_duration, current_duration)
            else:
                current_duration = 0
        return f"{max_duration} 天"

    # ============================================================
    # 3. 报告打印
    # ============================================================

    def report(self, benchmark_metrics: dict = None):
        """打印完整回测报告"""
        metrics = self.analyze()

        print("\n" + "=" * 65)
        print("                    回测绩效报告")
        print("=" * 65)

        # —— 收益指标 ——
        print(f"\n  {'─' * 20} 收益指标 {'─' * 20}")
        for k in ['总收益率', '年化收益(CAGR)', '累计净值']:
            print(f"  {k:<25} {metrics.get(k, 'N/A'):>15}")

        # —— 风险指标 ——
        print(f"\n  {'─' * 20} 风险指标 {'─' * 20}")
        for k in ['年化波动率', '下行波动率', '最大回撤', '最大回撤持续天数']:
            print(f"  {k:<25} {metrics.get(k, 'N/A'):>15}")

        # —— 风险调整收益 ——
        print(f"\n  {'─' * 20} 风险调整收益 {'─' * 20}")
        for k in ['夏普比率(Sharpe)', '索提诺比率(Sortino)', '卡尔玛比率(Calmar)']:
            print(f"  {k:<25} {metrics.get(k, 'N/A'):>15}")

        # —— 交易统计 ——
        print(f"\n  {'─' * 20} 交易统计 {'─' * 20}")
        for k in ['总交易次数', '胜率', '盈亏比', '盈利因子(Profit Factor)',
                   '平均单笔盈亏', '平均盈利', '平均亏损', '平均持仓天数', '年化换手率']:
            print(f"  {k:<25} {metrics.get(k, 'N/A'):>15}")

        # —— 对比基准 ——
        if benchmark_metrics:
            print(f"\n  {'─' * 20} 基准对比 {'─' * 20}")
            print(f"  {'指标':<25} {'策略':>15} {'基准(沪深300)':>18}")
            print(f"  {'─' * 45}")
            compares = ['总收益率', '年化收益(CAGR)', '年化波动率', '夏普比率(Sharpe)', '最大回撤']
            for k in compares:
                s_val = metrics.get(k, 'N/A')
                b_val = benchmark_metrics.get(k, 'N/A')
                print(f"  {k:<25} {s_val:>15} {b_val:>18}")

        print("\n" + "=" * 65)

        # —— 卖出原因统计 ——
        if self.trades:
            print(f"\n  {'─' * 20} 卖出原因分布 {'─' * 20}")
            reasons = {}
            for t in self.trades:
                r = t.get('reason', '未知')
                reasons[r] = reasons.get(r, 0) + 1
            for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
                print(f"  {r:<25} {c:>8} 次  ({c/len(self.trades)*100:.1f}%)")

        return metrics

    # ============================================================
    # 4. 导出
    # ============================================================

    def export(self, output_dir: str = None):
        """导出回测结果到 CSV"""
        if output_dir is None:
            output_dir = Path(__file__).parent.parent / "output"
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)

        # 每日净值
        if self.daily_stats is not None:
            self.daily_stats.to_csv(output_dir / "daily_equity.csv")

        # 交易记录
        if self.trades:
            pd.DataFrame(self.trades).to_csv(output_dir / "trades.csv", index=False)

        print(f"[导出] 结果已保存到 {output_dir}")


# ============================================================
# 二、便捷函数
# ============================================================

def run_full_backtest(stock_list: list = None,
                      start_date: str = '20200101',
                      end_date: str = '20240601',
                      config: dict = None,
                      verbose: bool = True) -> Tuple[BacktestEngine, pd.DataFrame]:
    """
    一键运行完整回测流程

    参数:
        stock_list: 股票池（默认沪深300）
        start_date: 起始日期
        end_date:   结束日期
        config:     回测配置
        verbose:    是否打印进度

    返回:
        (engine, daily_stats)
    """
    from data_engine import init_tushare, download_daily, download_index_daily, get_stock_pool, load_multi_stock_data
    from factor_engine import compute_all_factors

    # 初始化
    init_tushare()

    # 获取股票池
    if stock_list is None:
        stock_list = get_stock_pool('hs300')

    # 下载数据
    if verbose:
        print(f"下载 {len(stock_list)} 只股票数据...")
    stock_data = load_multi_stock_data(stock_list, start_date, end_date)

    # 计算因子
    if verbose:
        print("计算因子...")
    for i, (code, df) in enumerate(stock_data.items()):
        stock_data[code] = compute_all_factors(df)
        if verbose and i % 50 == 49:
            print(f"  因子计算: {i+1}/{len(stock_data)}")

    # 下载指数数据
    if verbose:
        print("下载指数数据...")
    index_df = download_index_daily('000001.SH', start_date, end_date)

    # 运行回测
    if verbose:
        print("运行回测...")
    engine = BacktestEngine(config)
    daily_stats = engine.run(stock_data, index_df)

    return engine, daily_stats


# ============================================================
# 三、Walk-forward 验证
# ============================================================

def walk_forward_analysis(stock_data: dict, index_df: pd.DataFrame,
                          config: dict = None,
                          train_years: int = 2,
                          test_years: int = 1,
                          step_months: int = 6,
                          verbose: bool = True) -> pd.DataFrame:
    """
    Walk-forward 样本外验证框架

    参数:
        stock_data:   {code: DataFrame(含因子列)} 完整数据
        index_df:     指数数据（同上时间范围）
        config:       基础回测配置
        train_years:  训练窗口长度（年），默认 2
        test_years:   测试窗口长度（年），默认 1
        step_months:  滑动步长（月），默认 6
        verbose:      是否打印进度

    返回:
        DataFrame: 每期样本外表现汇总

    原理：
      模拟真实交易场景——你只能用过去的数据做决策，不可能预知未来。
      每个窗口用训练集优化参数 → 固定参数在测试集上验证 → 汇总OOS表现。

    ┌─────────────┬──────────┐
    │  训练(2年)   │ 测试(1年) │
    └─────────────┴──────────┘
              → 滑动 6 个月 →
         ┌─────────────┬──────────┐
         │  训练(2年)   │ 测试(1年) │
         └─────────────┴──────────┘

    面试考点：
      Q: 为什么 Walk-forward 比一次回测更可信？
      A: 一次回测只给你一个数字但无法判断多大概率是"运气好"。
         Walk-forward 给你 N 个独立的 OOS 区间表现，可以算夏普均值/标准差，
         评估策略在不同市场环境下的稳定性。
         相当于 N 次独立实验——结论更稳健。
    """
    if not stock_data or index_df is None or index_df.empty:
        print("[WF] 数据不足，无法执行 Walk-forward 验证")
        return pd.DataFrame()

    # 找出数据的时间范围
    all_dates = sorted(index_df.index)
    if len(all_dates) < (train_years + test_years) * 252:
        print(f"[WF] 数据长度不足 (需≥{(train_years+test_years)*252}天，实际{len(all_dates)}天)")
        return pd.DataFrame()

    start_date = all_dates[0]
    end_date = all_dates[-1]
    total_days = (end_date - start_date).days
    train_days = train_years * 252
    test_days = test_years * 252
    step_days = step_months * 21  # 约21个交易日/月

    if config is None:
        config = {}

    windows = []
    current_start = start_date
    while True:
        train_start = current_start
        train_end = train_start + pd.Timedelta(days=train_years * 365)
        test_start = train_end
        test_end = test_start + pd.Timedelta(days=test_years * 365)

        if test_end > end_date:
            break

        windows.append({
            'train_start': train_start,
            'train_end': train_end,
            'test_start': test_start,
            'test_end': test_end,
        })
        current_start += pd.Timedelta(days=step_months * 30)

    if len(windows) < 2:
        print(f"[WF] 窗口数不足 (只有{len(windows)}个，需≥2)")
        return pd.DataFrame()

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Walk-forward 样本外验证")
        print(f"{'='*60}")
        print(f"  总数据: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
        print(f"  窗口数: {len(windows)}")
        print(f"  训练窗: {train_years}年 | 测试窗: {test_years}年 | 步长: {step_months}月")
        print()

    oos_results = []

    for w_idx, w in enumerate(windows):
        train_s = w['train_start'].strftime('%Y%m%d')
        test_s = w['test_start'].strftime('%Y%m%d')
        test_e = w['test_end'].strftime('%Y%m%d')

        if verbose:
            print(f"  [{w_idx+1}/{len(windows)}] "
                  f"训练:{w['train_start'].strftime('%Y-%m')}~{w['train_end'].strftime('%Y-%m')} "
                  f"→ 测试:{w['test_start'].strftime('%Y-%m')}~{w['test_end'].strftime('%Y-%m')}")

        # 筛选测试窗口的数据
        test_stock_data = {}
        for code, df in stock_data.items():
            mask = (df.index >= w['test_start']) & (df.index <= w['test_end'])
            test_df = df.loc[mask].copy()
            if len(test_df) >= 60:  # 至少60个交易日
                test_stock_data[code] = test_df

        if len(test_stock_data) < 10:
            if verbose:
                print(f"    → 有效股票不足10只，跳过")
            continue

        # 筛选指数的测试区间
        idx_mask = (index_df.index >= w['test_start']) & (index_df.index <= w['test_end'])
        test_index = index_df.loc[idx_mask].copy()

        if len(test_index) < 30:
            if verbose:
                print(f"    → 指数数据不足30天，跳过")
            continue

        # 运行回测（用默认参数，不做训练期超参优化——生产环境会在这里做）
        try:
            engine = BacktestEngine(config)
            stats = engine.run(test_stock_data, test_index)

            if stats is not None and len(stats) > 0:
                metrics = engine.analyze()
                oos_results.append({
                    'window': w_idx + 1,
                    'train_start': w['train_start'].strftime('%Y-%m-%d'),
                    'test_start': w['test_start'].strftime('%Y-%m-%d'),
                    'test_end': w['test_end'].strftime('%Y-%m-%d'),
                    'test_days': len(stats),
                    'cagr': _parse_pct(metrics.get('年化收益(CAGR)', '0%')),
                    'volatility': _parse_pct(metrics.get('年化波动率', '0%')),
                    'sharpe': float(metrics.get('夏普比率(Sharpe)', '0')),
                    'max_dd': _parse_pct(metrics.get('最大回撤', '0%')),
                    'calmar': float(metrics.get('卡尔玛比率(Calmar)', '0')),
                    'win_rate': _parse_pct(metrics.get('胜率', '0%')),
                    'total_return': _parse_pct(metrics.get('总收益率', '0%')),
                    'trades': int(metrics.get('总交易次数', 0)),
                    'profit_factor': float(metrics.get('盈利因子(Profit Factor)', '0')),
                })
                if verbose:
                    print(f"    → CAGR:{oos_results[-1]['cagr']:.1%} "
                          f"Sharpe:{oos_results[-1]['sharpe']:.2f} "
                          f"MaxDD:{oos_results[-1]['max_dd']:.1%} "
                          f"胜率:{oos_results[-1]['win_rate']:.0%}")
        except Exception as e:
            if verbose:
                print(f"    → 回测失败: {e}")

    if not oos_results:
        print("[WF] 无有效OOS结果")
        return pd.DataFrame()

    df_wf = pd.DataFrame(oos_results)

    # 汇总统计
    if verbose:
        print(f"\n  {'─'*50}")
        print(f"  Walk-forward 汇总 (共 {len(df_wf)} 个OOS窗口)")
        print(f"  {'─'*50}")
        print(f"  {'指标':<20} {'均值':>10} {'中位数':>10} {'最小':>10} {'最大':>10}")
        print(f"  {'─'*50}")
        for col, name in [('cagr', 'CAGR'), ('sharpe', 'Sharpe'),
                          ('max_dd', 'MaxDD'), ('win_rate', '胜率'),
                          ('calmar', 'Calmar'), ('profit_factor', 'ProfitFactor')]:
            vals = df_wf[col].dropna()
            if len(vals) > 0:
                print(f"  {name:<20} {vals.mean():>10.2%} {vals.median():>10.2%} "
                      f"{vals.min():>10.2%} {vals.max():>10.2%}")

        # 稳定性指标：CAGR的变异系数（CV = std/mean）
        cagr_vals = df_wf['cagr'].dropna()
        if len(cagr_vals) > 1 and abs(cagr_vals.mean()) > 0.001:
            cagr_cv = cagr_vals.std() / abs(cagr_vals.mean())
            print(f"\n  CAGR 稳定性(CV): {cagr_cv:.2f} (<0.5=稳定, <1.0=可接受, >1.5=不稳定)")

        # 统计 Sharpe > 1 的窗口占比
        sharpe_gt1 = (df_wf['sharpe'] > 1.0).mean()
        print(f"  Sharpe>1 窗口占比: {sharpe_gt1:.0%}")

    return df_wf


def _parse_pct(s: str) -> float:
    """解析百分比字符串为 float，如 '15.3%' → 0.153"""
    if isinstance(s, (int, float)):
        return float(s)
    try:
        if s.endswith('%'):
            return float(s[:-1]) / 100
        return float(s)
    except (ValueError, TypeError):
        return 0.0


# ============================================================
# 四、测试
# ============================================================

if __name__ == "__main__":
    # 快速测试：只用少量股票
    test_stocks = ['000001.SZ', '000002.SZ', '600036.SH', '600519.SH', '000858.SZ',
                   '601318.SH', '000333.SZ', '600900.SH', '601166.SH', '600276.SH']

    engine, stats = run_full_backtest(
        stock_list=test_stocks,
        start_date='20200101',
        end_date='20240601',
        verbose=True
    )

    engine.report()
    engine.export()
