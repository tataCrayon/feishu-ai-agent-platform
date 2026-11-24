"""digital-oracle-agent handler — 数字先知（市场数据概率预测），独立 Agent 不依赖 cc-connect。

能力由 bot/skills/digital-oracle/ 自包含包承载（引擎 vendoring 自
komako-workshop/digital-oracle，MIT），本 Handler 是编排壳：
1. 延迟加载引擎（sys.path 注入 skill 目录，一次性构建全部 provider）
2. Function Calling 单工具 gather_signals：LLM 按问题选 ≥3 个信号，
   一次调用并行拉取（引擎 gather() 线程池，单源失败不影响其他）
3. 信号结果 → 紧凑文本回填 LLM，由它按数字先知输出模板组织报告

同步阻塞取数跑线程池（同 webportal_handler 先例）；工具数据只读公开 API，
无凭据、无写操作。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from app.agents.handlers import AgentHandler, ToolResult

logger = logging.getLogger("agents.handlers.digital_oracle")

# ── Skill 包定位 ─────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent  # handlers → feishu-agent-platform/
ORACLE_SKILL_DIR = PROJECT_DIR / "bot" / "skills" / "digital-oracle"

# 单次 gather_signals 的信号数上限（防 LLM 失控拉满全菜单）
_MAX_SIGNALS = 12
# 单信号 wall-clock 上限（秒）——引擎 gather(timeout_seconds) 整批兜底
_GATHER_TIMEOUT_SEC = 45.0


def _load_engine():
    """导入 digital_oracle 包（skill 目录一次性注入 sys.path）。

    返回 (providers_dict, gather)。skill 目录不满足时抛 ImportError，
    由自动发现兜底（warning 不阻断启动）。
    """
    if not ORACLE_SKILL_DIR.is_dir():
        raise ImportError(f"digital-oracle skill 目录不存在: {ORACLE_SKILL_DIR}")
    skill_str = str(ORACLE_SKILL_DIR)
    if skill_str not in sys.path:
        sys.path.insert(0, skill_str)
    from digital_oracle import gather
    from digital_oracle.providers import (
        BisProvider,
        CftcCotProvider,
        CoinGeckoProvider,
        DeribitProvider,
        EastmoneyProvider,
        EdgarProvider,
        FearGreedProvider,
        KalshiProvider,
        PolymarketProvider,
        USTreasuryProvider,
        WebSearchProvider,
        WorldBankProvider,
        YahooPriceProvider,
        YFinanceProvider,
    )

    providers = {
        "polymarket": PolymarketProvider(),
        "kalshi": KalshiProvider(),
        "price_history": YahooPriceProvider(),
        "options_chain": YFinanceProvider(),
        "yield_curve": USTreasuryProvider(),
        "cftc_cot": CftcCotProvider(),
        "edgar_insider": EdgarProvider(),
        "coingecko": CoinGeckoProvider(),
        "deribit": DeribitProvider(),
        "fear_greed": FearGreedProvider(),
        "eastmoney_quote": EastmoneyProvider(),
        "eastmoney_fund_flow": EastmoneyProvider(),
        "eastmoney_sector": EastmoneyProvider(),
        "bis_rates": BisProvider(),
        "bis_credit_gap": BisProvider(),
        "worldbank": WorldBankProvider(),
        "web_search": WebSearchProvider(),
    }
    return providers, gather


# ── 信号执行（同步，跑线程池）────────────────────────────────────


def _q(*items: Any) -> tuple:
    """过滤 None 后构造 tuple（dataclass 元组字段传参用）。"""
    return tuple(i for i in items if i is not None)


def _run_signal(providers: dict, stype: str, spec: dict[str, Any]) -> Any:
    """按 spec 执行单个信号取数，返回引擎原生 dataclass（失败抛异常）。"""
    spec = spec or {}

    if stype == "polymarket":
        from digital_oracle import PolymarketEventQuery

        return providers["polymarket"].list_events(PolymarketEventQuery(
            slug_contains=spec.get("query"),
            title_contains=spec.get("query"),
            limit=int(spec.get("limit", 8)),
        ))

    if stype == "kalshi":
        from digital_oracle import KalshiMarketQuery

        return providers["kalshi"].list_markets(KalshiMarketQuery(
            series_ticker=spec.get("series_ticker"),
            event_ticker=spec.get("event_ticker"),
            limit=int(spec.get("limit", 10)),
        ))

    if stype == "price_history":
        from digital_oracle import PriceHistoryQuery

        return providers["price_history"].get_history(PriceHistoryQuery(
            symbol=str(spec["symbol"]),
            interval=str(spec.get("interval", "d")),
            limit=int(spec.get("limit", 30)),
        ))

    if stype == "options_chain":
        from digital_oracle import OptionsChainQuery

        return providers["options_chain"].get_chain(OptionsChainQuery(
            ticker=str(spec["ticker"]),
            expiration=spec.get("expiration"),
        ))

    if stype == "yield_curve":
        return providers["yield_curve"].latest_yield_curve()

    if stype == "cftc_cot":
        from digital_oracle import CftcCotQuery

        return providers["cftc_cot"].list_reports(CftcCotQuery(
            commodity_name=spec.get("commodity"),
            limit=int(spec.get("limit", 6)),
        ))

    if stype == "edgar_insider":
        from digital_oracle import EdgarInsiderQuery

        return providers["edgar_insider"].get_insider_transactions(
            EdgarInsiderQuery(ticker=str(spec["ticker"]), limit=int(spec.get("limit", 10)))
        )

    if stype == "coingecko":
        from digital_oracle import CoinGeckoPriceQuery

        if spec.get("market"):
            from digital_oracle import CoinGeckoMarketQuery

            return providers["coingecko"].get_market(CoinGeckoMarketQuery(
                vs_currency="usd", per_page=int(spec.get("limit", 10)),
            ))
        return providers["coingecko"].get_prices(CoinGeckoPriceQuery(
            coin_ids=_q(*spec.get("coin_ids", ("bitcoin", "ethereum"))),
        ))

    if stype == "deribit":
        currency = str(spec.get("currency", "BTC"))
        if spec.get("kind") == "options":
            from digital_oracle import DeribitOptionChainQuery

            return providers["deribit"].get_option_chain(DeribitOptionChainQuery(
                currency=currency, expiration_label=spec.get("expiration_label"),
            ))
        from digital_oracle import DeribitFuturesCurveQuery

        return providers["deribit"].get_futures_term_structure(
            DeribitFuturesCurveQuery(currency=currency)
        )

    if stype == "fear_greed":
        return providers["fear_greed"].get_index()

    if stype == "eastmoney_quote":
        from digital_oracle import EastmoneyQuoteQuery

        return providers["eastmoney_quote"].get_quote(
            EastmoneyQuoteQuery(symbol=str(spec["symbol"]))
        )

    if stype == "eastmoney_fund_flow":
        from digital_oracle import EastmoneyFundFlowQuery

        return providers["eastmoney_fund_flow"].get_fund_flow(EastmoneyFundFlowQuery(
            symbol=str(spec["symbol"]), limit=int(spec.get("limit", 10)),
        ))

    if stype == "eastmoney_sector":
        from digital_oracle import EastmoneySectorFlowQuery

        return providers["eastmoney_sector"].list_sector_fund_flow(
            EastmoneySectorFlowQuery(
                kind=str(spec.get("kind", "industry")), limit=int(spec.get("limit", 15)),
            )
        )

    if stype == "bis_rates":
        from digital_oracle import BisRateQuery

        return providers["bis_rates"].get_policy_rates(BisRateQuery(
            countries=_q(*spec.get("countries", ("US", "CN"))),
            start_year=int(spec.get("start_year", 2023)),
        ))

    if stype == "bis_credit_gap":
        from digital_oracle import BisCreditGapQuery

        return providers["bis_credit_gap"].get_credit_to_gdp(BisCreditGapQuery(
            countries=_q(*spec.get("countries", ("US", "CN"))),
            start_year=int(spec.get("start_year", 2019)),
        ))

    if stype == "worldbank":
        from digital_oracle import WorldBankQuery

        return providers["worldbank"].get_indicator(WorldBankQuery(
            indicator=str(spec["indicator"]),
            countries=_q(*spec.get("countries", ("US", "CN"))),
        ))

    if stype == "web_search":
        from digital_oracle import WebSearchQuery

        return providers["web_search"].search(WebSearchQuery(
            query=str(spec["query"]), max_results=int(spec.get("max_results", 4)),
        ))

    raise ValueError(f"未知信号类型: {stype}（见 prompt 信号菜单）")


# ── 结果渲染（dataclass → LLM 可读紧凑文本）──────────────────────


def _fmt(v: Any) -> str:
    """数值 → 紧凑文本（None → '—'，浮点保留 2 位）。"""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _render_signal(stype: str, result: Any) -> str:
    """单信号结果 → 紧凑文本块（超长截断，控制回填 LLM 的 token）。"""
    lines: list[str] = []

    if stype == "polymarket" and isinstance(result, list):
        for ev in result[:8]:
            m = ev.primary_market()
            if not m:
                continue
            lines.append(
                f"  • {ev.title} | YES={_pct(m.yes_probability)} "
                f"| 24h量={_fmt(m.volume_24hr)} | 截止={_fmt(m.end_date)}"
            )
    elif stype == "kalshi" and isinstance(result, list):
        for m in result[:8]:
            lines.append(
                f"  • {m.ticker} | {m.title} | YES={_pct(m.yes_probability)} "
                f"| 量={_fmt(m.volume)}"
            )
    elif stype == "price_history":
        bars = getattr(result, "bars", None) or []
        latest = getattr(result, "latest", None)
        if latest is not None:
            lines.append(
                f"  最新({_fmt(getattr(latest, 'date', ''))}): close={_fmt(latest.close)} "
                f"high={_fmt(latest.high)} low={_fmt(latest.low)} vol={_fmt(latest.volume)}"
            )
        closes = [b.close for b in bars if b.close is not None]
        if len(closes) >= 2:
            chg = (closes[-1] - closes[0]) / closes[0] * 100
            lines.append(f"  区间涨跌({len(closes)}根bar): {chg:+.2f}%  首收={_fmt(closes[0])} 末收={_fmt(closes[-1])}")
        for b in bars[-5:]:
            lines.append(f"  {_fmt(b.date)} close={_fmt(b.close)} vol={_fmt(b.volume)}")
    elif stype == "options_chain":
        lines.append(f"  标的价={_fmt(getattr(result, 'underlying_price', None))} 到期={_fmt(getattr(result, 'expiration', ''))}")
        lines.append(f"  ATM IV={_pct(getattr(result, 'atm_iv', None))} "
                     f"put/call(量)={_fmt(getattr(result, 'put_call_volume_ratio', None))} "
                     f"put/call(OI)={_fmt(getattr(result, 'put_call_oi_ratio', None))} "
                     f"总OI={_fmt(getattr(result, 'total_open_interest', None))}")
        im = result.implied_move() if hasattr(result, "implied_move") else None
        if im is not None:
            lines.append(f"  隐含波动区间=±{im * 100:.2f}%")
        atm_call, atm_put = getattr(result, "atm_call", None), getattr(result, "atm_put", None)
        if atm_call:
            lines.append(f"  ATM call: strike={_fmt(atm_call.strike)} IV={_pct(atm_call.implied_volatility)} "
                         f"delta={_fmt(getattr(atm_call.greeks, 'delta', None) if atm_call.greeks else None)}")
        if atm_put:
            lines.append(f"  ATM put : strike={_fmt(atm_put.strike)} IV={_pct(atm_put.implied_volatility)} "
                         f"delta={_fmt(getattr(atm_put.greeks, 'delta', None) if atm_put.greeks else None)}")
    elif stype == "yield_curve":
        pts = getattr(result, "points", ())
        date = getattr(result, "date", "")
        lines.append(f"  日期={_fmt(date)}")
        for p in pts:
            lines.append(f"  {p.tenor}={p.value}")
        for long_t, short_t in (("10Y", "2Y"), ("10Y", "3M")):
            sp = result.spread(long_t, short_t)
            if sp is not None:
                lines.append(f"  {long_t}-{short_t}利差={sp:+.2f}pp" + ("（倒挂=衰退信号）" if sp < 0 else ""))
    elif stype == "cftc_cot" and isinstance(result, list):
        for r in result[:6]:
            lines.append(
                f"  {r.report_date} | {r.market_name[:40]} | managed_money净={r.mm_net:+,} "
                f"| producer净={(r.prod_long - r.prod_short):+,} | OI={r.open_interest:,}"
            )
    elif stype == "edgar_insider":
        txs = getattr(result, "transactions", None) or []
        lines.append(f"  内部人 Form 4（最近 {len(txs)} 笔）:")
        for t in txs[:10]:
            lines.append(f"  {_fmt(getattr(t, 'filing_date', ''))} {_fmt(getattr(t, 'reporter', ''))} "
                         f"{_fmt(getattr(t, 'transaction_type', ''))} {_fmt(getattr(t, 'shares', ''))}股 "
                         f"@{_fmt(getattr(t, 'price', ''))}")
    elif stype == "coingecko":
        if isinstance(result, list):
            for c in result[:8]:
                lines.append(f"  {c.coin_id}: ${_fmt(c.price_usd)} 市值=${_fmt(c.market_cap_usd)} "
                             f"24h={_fmt(c.price_change_24h_pct)}%")
        else:
            lines.append(f"  总市值=${_fmt(getattr(result, 'total_market_cap_usd', None))} "
                         f"BTC占比={_fmt(getattr(result, 'btc_dominance_pct', None))}% "
                         f"24h变化={_fmt(getattr(result, 'market_cap_change_24h_pct', None))}%")
    elif stype == "deribit":
        pts = getattr(result, "points", None)
        if pts:
            for p in pts[:8]:
                lines.append(f"  {p.instrument_name} mark={_fmt(p.mark_price)} "
                             f"年化basis={_fmt(getattr(p, 'annualized_basis_vs_perpetual', None))}%")
        else:
            calls = getattr(result, "calls", ()) or ()
            puts = getattr(result, "puts", ()) or ()
            lines.append(f"  到期={_fmt(getattr(result, 'expiration', ''))} calls={len(calls)} puts={len(puts)}")
            for c in calls[:3] + puts[:3]:
                lines.append(f"  {c.option_type} strike={_fmt(c.strike)} IV={_pct(c.implied_volatility)} "
                             f"OI={_fmt(c.open_interest)}")
    elif stype == "fear_greed":
        lines.append(f"  score={_fmt(result.score)} ({_fmt(result.rating)}) "
                     f"昨收={_fmt(result.previous_close)} 周前={_fmt(result.one_week_ago)} "
                     f"月前={_fmt(result.one_month_ago)}")
    elif stype == "eastmoney_quote":
        lines.append(f"  {result.name}({result.symbol}) last={_fmt(result.last)} "
                     f"涨跌={_fmt(result.change_pct)}% 换手={_fmt(result.turnover_rate_pct)}% "
                     f"PE(TTM)={_fmt(result.pe_ttm)} PB={_fmt(result.pb)} "
                     f"市值={_fmt(result.market_cap_cny)}")
    elif stype == "eastmoney_fund_flow":
        lines.append(f"  {getattr(result, 'name', '')} 近 {len(getattr(result, 'days', ()) or ())} 日主力净流入:")
        for d in (getattr(result, "days", ()) or ())[-10:]:
            lines.append(f"  {d.date} 主力={_fmt(d.main_net)} ({_fmt(d.main_net_pct)}%) "
                         f"超大={_fmt(d.extra_large_net)} 散户(小单)={_fmt(d.small_net)}")
    elif stype == "eastmoney_sector" and isinstance(result, list):
        for s in result[:15]:
            lines.append(f"  {s.name} 涨跌={_fmt(s.change_pct)}% 主力净流入={_fmt(s.main_net_cny)} "
                         f"({_fmt(s.main_net_pct)}%)")
    elif stype == "bis_rates" and isinstance(result, list):
        for r in result[-12:]:
            lines.append(f"  {_fmt(getattr(r, 'country', ''))} {_fmt(getattr(r, 'period', ''))} "
                         f"政策利率={_fmt(getattr(r, 'rate_pct', None))}%")
    elif stype == "bis_credit_gap" and isinstance(result, list):
        for r in result[-12:]:
            lines.append(f"  {_fmt(getattr(r, 'country', ''))} {_fmt(getattr(r, 'period', ''))} "
                         f"信贷/GDP缺口={_fmt(getattr(r, 'gap_pct', None))}pp")
    elif stype == "worldbank":
        obs = getattr(result, "observations", None) or []
        lines.append(f"  指标={_fmt(getattr(result, 'indicator_name', ''))}")
        for o in obs[-10:]:
            lines.append(f"  {_fmt(getattr(o, 'country', ''))} {_fmt(getattr(o, 'year', ''))} "
                         f"={_fmt(getattr(o, 'value', None))}")
    elif stype == "web_search":
        lines.append(str(getattr(result, "text", lambda: result)())[:800])

    text = "\n".join(lines) if lines else f"  （{stype} 无可用数据）"
    return text[:2000]


def _render_results(
    results: dict[str, Any], errors: dict[str, BaseException], stypes: dict[str, str]
) -> str:
    """全部信号 → 回填 LLM 的文本（成功块 + 失败块，失败明示不编造）。

    stypes: 信号名 → spec.type（渲染分支按 type 匹配，key 是 LLM 起的中文名）。
    """
    parts: list[str] = []
    for key, val in results.items():
        stype = stypes.get(key, key.split(":", 1)[0])
        parts.append(f"### 信号 [{key}]（{stype}）\n{_render_signal(stype, val)}")
    if errors:
        parts.append("### 失败信号（报告中必须如实标注，禁止编造数值）")
        for key, err in errors.items():
            parts.append(f"  - [{key}] {type(err).__name__}: {str(err)[:160]}")
    if not parts:
        return "⚠️ 全部信号取数失败，无数据可用。"
    return "\n\n".join(parts)[:14000]


# ── Handler ──────────────────────────────────────────────────────

GATHER_SIGNALS_TOOL: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "gather_signals",
            "description": "并行拉取市场交易数据信号（预测市场/价格/期权/收益率曲线/持仓/资金流等）。"
                "signals 为 {信号名: {type, ...参数}} 映射，一次调用全部并行执行，"
                "单源失败不影响其他。选 ≥3 个独立维度信号后再下结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "signals": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "polymarket", "kalshi", "price_history", "options_chain",
                                        "yield_curve", "cftc_cot", "edgar_insider", "coingecko",
                                        "deribit", "fear_greed", "eastmoney_quote",
                                        "eastmoney_fund_flow", "eastmoney_sector", "bis_rates",
                                        "bis_credit_gap", "worldbank", "web_search",
                                    ],
                                    "description": "信号类型（见 prompt 信号菜单）",
                                },
                            },
                            "description": "type + 该类型的参数（query/symbol/ticker/coin_ids 等）",
                        },
                        "description": "信号清单，如 {\"黄金月线\": {\"type\": \"price_history\", \"symbol\": \"GC=F\", \"interval\": \"m\", \"limit\": 12}}",
                    },
                },
                "required": ["signals"],
            },
        },
    },
]


class DigitalOracleHandler(AgentHandler):
    """数字先知处理器（digital-oracle 引擎壳）。"""

    agent_name = "digital-oracle-agent"
    tools = GATHER_SIGNALS_TOOL

    MAX_TOOL_ROUNDS = 3  # 通常一轮 gather_signals 即够，留一轮补数

    def __init__(self, config):
        super().__init__(config)
        self._providers: dict | None = None
        self._gather = None
        self._engine_err: str = ""

    # ── 引擎加载 ─────────────────────────────────────────────────

    def _get_engine(self):
        """延迟加载引擎；失败缓存错误避免每次重试。"""
        if self._providers is not None:
            return self._providers, self._gather
        if self._engine_err:
            raise RuntimeError(self._engine_err)
        try:
            providers, gather = _load_engine()
            self._providers, self._gather = providers, gather
            return providers, gather
        except Exception as e:
            self._engine_err = f"digital-oracle 引擎加载失败: {e}"
            logger.error(self._engine_err, exc_info=True)
            raise RuntimeError(self._engine_err) from e

    # ── 工具执行（同步引擎 → 线程池）────────────────────────────

    def _run_gather_sync(self, signals: dict[str, dict[str, Any]]) -> str:
        providers, gather = self._get_engine()
        if not isinstance(signals, dict) or not signals:
            raise ValueError("signals 不能为空（至少选 1 个信号，建议 ≥3 个独立维度）")
        if len(signals) > _MAX_SIGNALS:
            signals = dict(list(signals.items())[:_MAX_SIGNALS])

        tasks: dict[str, Any] = {}
        stypes: dict[str, str] = {}
        for key, spec in signals.items():
            spec = spec if isinstance(spec, dict) else {}
            stype = str(spec.get("type", "")).strip()
            stypes[key] = stype
            if not stype:
                tasks[key] = self._raise_missing_type
                continue
            tasks[key] = (lambda s=stype, sp=spec: _run_signal(providers, s, sp))

        gr = gather(tasks, timeout_seconds=_GATHER_TIMEOUT_SEC)
        return _render_results(gr.results, gr.errors, stypes)

    @staticmethod
    def _raise_missing_type() -> None:
        raise ValueError("信号缺少 type 字段")

    def _tool_narrative(self, tool_name: str, args: dict, done: bool) -> str:
        """数字先知域工具事件 → 叙事化文案。"""
        n = len(args.get("signals", {}) or {}) if isinstance(args, dict) else 0
        if done:
            return f"{n} 路市场信号已取回" if n else "市场信号已取回"
        return f"正在并行拉取 {n} 路市场信号（预测市场/价格/持仓…）" if n else "正在拉取市场信号"

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        if tool_name != "gather_signals":
            return ToolResult(error=f"未知工具: {tool_name}")
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, self._run_gather_sync, args.get("signals") or {}
            )
            return ToolResult(data=text)
        except Exception as e:
            logger.warning("gather_signals failed: %s", e)
            return ToolResult(error=str(e)[:200])


def build(config, ctx):
    """discovery 约定工厂。"""
    return DigitalOracleHandler(config)
