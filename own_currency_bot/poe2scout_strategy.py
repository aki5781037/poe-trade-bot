from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import currency_bot as bot


API_ROOT = "https://poe2scout.com/api"
REALM = "poe2"
DEFAULT_BASE_API_ID = "chaos"

NAME_ALIASES = {
    "混沌石": "chaos",
    "神圣石": "divine",
    "神聖石": "divine",
    "削切之兆": "omen-of-whittling",
    "無效石": "annul",
    "完美混沌石": "perfect-chaos-orb",
    "破裂石": "fracturing-orb",
    "完美崇高石": "perfect-exalted-orb",
    "幻像異界": "simulacrum",
    "左旋抺除之兆": "omen-of-sinistral-erasure",
    "右旋抺除之兆": "omen-of-dextral-erasure",
    "左旋廢止之兆": "omen-of-sinistral-annulment",
    "右旋廢止之兆": "omen-of-dextral-annulment",
    "光明之兆": "omen-of-light",
}


@dataclass
class ScoutCurrency:
    api_id: str
    text: str
    item_id: int


@dataclass
class PairStrategy:
    name: str
    base_name: str
    hourly_ratio: float
    volume_target: int
    volume_base: int
    buy_cost: int
    sell_return: int
    profit: int
    profit_pct: float
    epoch: int


@dataclass
class SnapshotCandidate:
    name: str
    api_id: str
    ratio: float
    volume_target: int
    volume_base: int
    highest_stock_target: int
    highest_stock_base: int
    buy_cost: int
    sell_return: int
    profit: int
    profit_pct: float


@dataclass
class ChainStrategy:
    divine_to_chaos: float
    omen_to_chaos: float
    omen_to_divine: float
    buy_omen_cost_chaos: int
    omen_count: int
    divine_in: int
    chaos_received: int
    chaos_spent: int
    chaos_left: int
    divine_out: int
    profit_divine: int
    profit_equiv_divine: float
    gold_fee: int
    gold_per_divine: float
    gold_per_equiv_divine: float
    epoch: int


def fetch_json(path: str) -> Any:
    url = f"{API_ROOT}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "OwnCurrencyBot/0.1"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def current_league() -> str:
    payload = fetch_json(f"/{REALM}/Leagues")
    leagues = payload.get("value", []) if isinstance(payload, dict) else payload
    current = next((league for league in leagues if league.get("IsCurrent")), None)
    if current:
        return str(current["Value"])
    default = next((league for league in leagues if league.get("Value") == "Runes of Aldur"), None)
    if default:
        return str(default["Value"])
    if not leagues:
        raise RuntimeError("POE2 Scout returned no leagues")
    return str(leagues[0]["Value"])


def league_path(league: str) -> str:
    return urllib.parse.quote(league, safe="")


def snapshot_pairs(league: str) -> list[dict[str, Any]]:
    return fetch_json(f"/{REALM}/Leagues/{league_path(league)}/SnapshotPairs")


def exchange_snapshot(league: str) -> dict[str, Any]:
    return fetch_json(f"/{REALM}/Leagues/{league_path(league)}/ExchangeSnapshot")


def currency_index(pairs: list[dict[str, Any]]) -> dict[str, ScoutCurrency]:
    index: dict[str, ScoutCurrency] = {}
    for pair in pairs:
        for key in ("CurrencyOne", "CurrencyTwo"):
            cur = pair.get(key) or {}
            api_id = str(cur.get("ApiId") or "")
            if not api_id or api_id in index:
                continue
            index[api_id] = ScoutCurrency(
                api_id=api_id,
                text=str(cur.get("Text") or api_id),
                item_id=int(cur.get("ItemId") or 0),
            )
    return index


def resolve_api_id(name: str) -> str:
    return NAME_ALIASES.get(name, name)


def pair_history(league: str, left_item_id: int, right_item_id: int, limit: int = 24) -> list[dict[str, Any]]:
    data = fetch_json(
        f"/{REALM}/Leagues/{league_path(league)}/Currencies/Pairs/"
        f"{left_item_id}/{right_item_id}/History?Limit={limit}"
    )
    return list(data.get("History") or [])


def hourly_ratio_from_history(history: list[dict[str, Any]]) -> tuple[float, int, int, int]:
    if not history:
        raise RuntimeError("no POE2 Scout history for pair")
    latest = history[0]
    data = latest["Data"]
    left = data["CurrencyOneData"]
    right = data["CurrencyTwoData"]
    left_price = float(left["RelativePrice"])
    right_price = float(right["RelativePrice"])
    if right_price <= 0:
        raise RuntimeError("invalid base relative price")
    ratio = left_price / right_price
    return (
        ratio,
        int(left.get("VolumeTraded") or 0),
        int(right.get("VolumeTraded") or 0),
        int(latest["Epoch"]),
    )


def latest_ratio(
    league: str,
    currencies: dict[str, ScoutCurrency],
    left_api_id: str,
    right_api_id: str,
) -> tuple[float, int]:
    left = currencies[left_api_id]
    right = currencies[right_api_id]
    history = pair_history(league, left.item_id, right.item_id, limit=1)
    ratio, _left_volume, _right_volume, epoch = hourly_ratio_from_history(history)
    return ratio, epoch


def build_pair_strategy(pair_name: str, base_name: str | None = None, league: str | None = None) -> PairStrategy:
    league = league or current_league()
    base_name = base_name or str(bot.config.get("BASE_CURRENCY", "混沌石"))
    pairs = snapshot_pairs(league)
    currencies = currency_index(pairs)

    target_api = resolve_api_id(pair_name)
    base_api = resolve_api_id(base_name)
    target = currencies.get(target_api)
    base = currencies.get(base_api)
    if not target:
        raise RuntimeError(f"POE2 Scout currency not found: {pair_name} ({target_api})")
    if not base:
        raise RuntimeError(f"POE2 Scout base currency not found: {base_name} ({base_api})")

    history = pair_history(league, target.item_id, base.item_id, limit=24)
    ratio, target_volume, base_volume, epoch = hourly_ratio_from_history(history)

    buy_step = int(bot.config.get("BUY_COMPETE_STEP", 1))
    min_profit = float(bot.config.get("SELL_PROFIT_MIN", 1.0))
    buy_cost = int(math.floor(ratio)) + buy_step
    sell_return = int(math.ceil(buy_cost * (1 + min_profit / 100.0)))
    profit = sell_return - buy_cost
    profit_pct = profit / buy_cost * 100 if buy_cost else 0.0
    return PairStrategy(
        name=pair_name,
        base_name=base_name,
        hourly_ratio=ratio,
        volume_target=target_volume,
        volume_base=base_volume,
        buy_cost=buy_cost,
        sell_return=sell_return,
        profit=profit,
        profit_pct=profit_pct,
        epoch=epoch,
    )


def build_omen_chain_strategy(league: str | None = None, prefer: str = "minimum_profit") -> ChainStrategy:
    league = league or current_league()
    pairs = snapshot_pairs(league)
    currencies = currency_index(pairs)
    for api_id in ("divine", "chaos", "omen-of-whittling"):
        if api_id not in currencies:
            raise RuntimeError(f"POE2 Scout currency not found: {api_id}")

    divine_to_chaos, epoch_dc = latest_ratio(league, currencies, "divine", "chaos")
    omen_to_chaos, epoch_oc = latest_ratio(league, currencies, "omen-of-whittling", "chaos")
    omen_to_divine, epoch_od = latest_ratio(league, currencies, "omen-of-whittling", "divine")
    epoch = max(epoch_dc, epoch_oc, epoch_od)

    buy_step = int(bot.config.get("BUY_COMPETE_STEP", 1))
    buy_omen_cost_chaos = max(1, int(math.floor(omen_to_chaos)) + buy_step)
    fee_per_order = int(bot.config.get("GOLD_FEE_PER_ORDER", 0))
    fee_per_omen = int(bot.config.get("GOLD_FEE_PER_OMEN", 800))
    max_omen = int(bot.config.get("STRATEGY_CHAIN_MAX_OMEN", 500))

    first_profitable: ChainStrategy | None = None
    best_efficiency: ChainStrategy | None = None
    for omen_count in range(1, max_omen + 1):
        chaos_spent = omen_count * buy_omen_cost_chaos
        divine_in = max(1, math.ceil(chaos_spent / divine_to_chaos))
        while math.floor(divine_in * divine_to_chaos) < chaos_spent:
            divine_in += 1
        chaos_received = int(math.floor(divine_in * divine_to_chaos))
        chaos_left = chaos_received - chaos_spent
        divine_out = int(math.floor(omen_count * omen_to_divine))
        profit_divine = divine_out - divine_in
        profit_equiv_divine = profit_divine + chaos_left / divine_to_chaos
        if profit_divine < 1:
            continue

        gold_fee = fee_per_order * 3 + fee_per_omen * omen_count
        gold_per_divine = gold_fee / profit_divine
        gold_per_equiv_divine = gold_fee / profit_equiv_divine if profit_equiv_divine > 0 else float("inf")
        candidate = ChainStrategy(
            divine_to_chaos=divine_to_chaos,
            omen_to_chaos=omen_to_chaos,
            omen_to_divine=omen_to_divine,
            buy_omen_cost_chaos=buy_omen_cost_chaos,
            omen_count=omen_count,
            divine_in=divine_in,
            chaos_received=chaos_received,
            chaos_spent=chaos_spent,
            chaos_left=chaos_left,
            divine_out=divine_out,
            profit_divine=profit_divine,
            profit_equiv_divine=profit_equiv_divine,
            gold_fee=gold_fee,
            gold_per_divine=gold_per_divine,
            gold_per_equiv_divine=gold_per_equiv_divine,
            epoch=epoch,
        )
        if first_profitable is None:
            first_profitable = candidate
        if best_efficiency is None or (candidate.gold_per_divine, candidate.divine_in) < (
            best_efficiency.gold_per_divine,
            best_efficiency.divine_in,
        ):
            best_efficiency = candidate

    selected = best_efficiency if prefer == "best_efficiency" else first_profitable
    if selected is None:
        raise RuntimeError("no profitable Divine -> Chaos -> Omen -> Divine chain found")
    return selected


def strategy_from_ratio(ratio: float) -> tuple[int, int, int, float]:
    buy_step = int(bot.config.get("BUY_COMPETE_STEP", 1))
    min_profit = float(bot.config.get("SELL_PROFIT_MIN", 1.0))
    buy_cost = max(1, int(math.floor(ratio)) + buy_step)
    sell_return = max(buy_cost + 1, int(math.ceil(buy_cost * (1 + min_profit / 100.0))))
    profit = sell_return - buy_cost
    profit_pct = profit / buy_cost * 100 if buy_cost else 0.0
    return buy_cost, sell_return, profit, profit_pct


def scan_snapshot_candidates(
    base_name: str | None = None,
    league: str | None = None,
    min_target_volume: int = 100,
    min_base_volume: int = 1000,
    min_target_stock: int = 10,
    min_profit_pct: float = 1.0,
    limit: int = 20,
) -> tuple[str, int | None, list[SnapshotCandidate]]:
    league = league or current_league()
    base_name = base_name or str(bot.config.get("BASE_CURRENCY", "混沌石"))
    base_api = resolve_api_id(base_name)
    pairs = snapshot_pairs(league)
    snapshot = exchange_snapshot(league)
    epoch = int(snapshot.get("Epoch") or 0) or None
    candidates: list[SnapshotCandidate] = []

    for pair in pairs:
        one = pair.get("CurrencyOne") or {}
        two = pair.get("CurrencyTwo") or {}
        one_data = pair.get("CurrencyOneData") or {}
        two_data = pair.get("CurrencyTwoData") or {}
        one_api = str(one.get("ApiId") or "")
        two_api = str(two.get("ApiId") or "")

        if one_api == base_api:
            target, target_data = two, two_data
            base_data = one_data
        elif two_api == base_api:
            target, target_data = one, one_data
            base_data = two_data
        else:
            continue

        target_api = str(target.get("ApiId") or "")
        if not target_api or target_api == base_api:
            continue

        try:
            target_price = float(target_data["RelativePrice"])
            base_price = float(base_data["RelativePrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if base_price <= 0:
            continue
        ratio = target_price / base_price
        if ratio < 2:
            continue

        target_volume = int(target_data.get("VolumeTraded") or 0)
        base_volume = int(base_data.get("VolumeTraded") or 0)
        target_stock = int(target_data.get("HighestStock") or 0)
        base_stock = int(base_data.get("HighestStock") or 0)
        if target_volume < min_target_volume or base_volume < min_base_volume:
            continue
        if target_stock < min_target_stock:
            continue

        buy_cost, sell_return, profit, profit_pct = strategy_from_ratio(ratio)
        if profit_pct < min_profit_pct:
            continue

        candidates.append(
            SnapshotCandidate(
                name=str(target.get("Text") or target_api),
                api_id=target_api,
                ratio=ratio,
                volume_target=target_volume,
                volume_base=base_volume,
                highest_stock_target=target_stock,
                highest_stock_base=base_stock,
                buy_cost=buy_cost,
                sell_return=sell_return,
                profit=profit,
                profit_pct=profit_pct,
            )
        )

    candidates.sort(key=lambda c: (c.profit_pct, c.volume_target, c.highest_stock_target), reverse=True)
    return league, epoch, candidates[:limit]


def strategy_report() -> str:
    league = current_league()
    lines = [f"POE2 Scout league: {league}"]
    for pair_name in bot.configured_pairs():
        strategy = build_pair_strategy(pair_name, league=league)
        ts = datetime.fromtimestamp(strategy.epoch).strftime("%Y-%m-%d %H:%M")
        lines.extend(
            [
                "",
                f"{strategy.name} / {strategy.base_name}",
                f"上一小时: {strategy.hourly_ratio:.4f} {strategy.base_name} / 1 {strategy.name} ({ts})",
                f"成交量: {strategy.volume_target} {strategy.name}, {strategy.volume_base} {strategy.base_name}",
                f"建议买入: 1 {strategy.name} = {strategy.buy_cost} {strategy.base_name}",
                f"建议卖出: 1 {strategy.name} = {strategy.sell_return} {strategy.base_name}",
                f"理论毛利: {strategy.profit} {strategy.base_name} ({strategy.profit_pct:.2f}%)",
            ]
        )
    lines.extend(["", chain_report(league)])
    return "\n".join(lines)


def chain_report(league: str | None = None) -> str:
    strategy = build_omen_chain_strategy(league=league, prefer="minimum_profit")
    efficient = build_omen_chain_strategy(league=league, prefer="best_efficiency")
    ts = datetime.fromtimestamp(strategy.epoch).strftime("%Y-%m-%d %H:%M")
    lines = [
        "完整链路: 神圣石 -> 混沌石 -> 削切之兆 -> 神圣石",
        f"数据时间: {ts}",
        f"Scout 比例: 1D={strategy.divine_to_chaos:.4f}C, 1 削切={strategy.omen_to_chaos:.4f}C, 1 削切={strategy.omen_to_divine:.4f}D",
        f"执行报价: 1 削切之兆 = {strategy.buy_omen_cost_chaos} 混沌石",
        f"最小赚 1D 批量: {strategy.divine_in}D -> {strategy.chaos_received}C -> 买 {strategy.omen_count} 个削切之兆, 花 {strategy.chaos_spent}C, 剩 {strategy.chaos_left}C -> 卖 {strategy.divine_out}D",
        f"理论利润: {strategy.profit_divine}D + {strategy.chaos_left}C = {strategy.profit_equiv_divine:.3f}D",
        f"金币手续费估算: {strategy.gold_fee} 金币/轮",
        f"赚 1D 的金币成本: {strategy.gold_per_divine:.0f} 金币（只按纯 D 利润）",
        f"按 D+C 折算后金币成本: {strategy.gold_per_equiv_divine:.0f} 金币/等价 1D",
    ]
    if efficient.omen_count != strategy.omen_count:
        lines.extend(
            [
                "",
                f"高资金效率参考: {efficient.divine_in}D -> 买 {efficient.omen_count} 个削切之兆 -> {efficient.divine_out}D",
                f"高资金效率利润: {efficient.profit_divine}D + {efficient.chaos_left}C = {efficient.profit_equiv_divine:.3f}D",
                f"高资金效率金币成本: {efficient.gold_per_divine:.0f} 金币/纯 1D",
            ]
        )
    return "\n".join(lines)


def scan_report() -> str:
    league, epoch, candidates = scan_snapshot_candidates()
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "unknown"
    lines = [
        f"POE2 Scout league: {league}",
        f"快照时间: {ts}",
        "筛选条件: base=混沌石, target_volume>=100, base_volume>=1000, target_stock>=10, profit>=1%",
        "",
        "候选交易对:",
    ]
    if not candidates:
        lines.append("无")
        return "\n".join(lines)
    for i, c in enumerate(candidates, 1):
        lines.extend(
            [
                f"{i}. {c.name} ({c.api_id})",
                f"   上小时比例: {c.ratio:.4f} 混沌石 / 1",
                f"   建议: 买 {c.buy_cost} 混沌 -> 卖 {c.sell_return} 混沌",
                f"   理论毛利: {c.profit} 混沌 ({c.profit_pct:.2f}%)",
                f"   成交量/库存: target {c.volume_target}/{c.highest_stock_target}, chaos {c.volume_base}/{c.highest_stock_base}",
            ]
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--scan" in sys.argv:
        print(scan_report())
    elif "--chain" in sys.argv:
        print(chain_report())
    else:
        print(strategy_report())
