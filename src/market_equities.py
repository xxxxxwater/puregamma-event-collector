"""US equities analytics (pure, no I/O): live tick semantics, stock/ETF classification,
universe selection, flows by stock / sector / industry / ETF category, anomalies.

Definitions: market/DESIGN.md §3 (C1, C2, C5, C6) and §5.
"""
import datetime as dt
import re

from market_analysis import ET, SECTOR_ZH, flow_value, fnum, money

STOCK_SUBTYPES = {"EQUITY_PRODUCT_SUBTYPE_COMMON_STOCK", "EQUITY_PRODUCT_SUBTYPE_ADR",
                  "EQUITY_PRODUCT_SUBTYPE_PREFERRED_STOCK", "EQUITY_PRODUCT_SUBTYPE_PREFERRED_ADR",
                  "EQUITY_PRODUCT_SUBTYPE_SPECIAL_STOCK"}
FUND_SUBTYPES = {"EQUITY_PRODUCT_SUBTYPE_ETF", "EQUITY_PRODUCT_SUBTYPE_SHARES_OF_BENEFICIAL_INTEREST"}
STATUS = {"market open": "open", "pre market": "pre", "pre-market": "pre", "after hours": "after",
          "after-hours": "after", "post market": "after", "market closed": "closed", "closed": "closed"}
SESSION_MINUTES = 390
INDUSTRY_ZH = {
    "Semiconductors": "半导体", "Computer Software: Programming Data Processing": "软件与数据处理",
    "Computer Software: Prepackaged Software": "套装软件", "Industrial Machinery/Components": "工业机械",
    "Auto Manufacturing": "汽车制造", "Biotechnology: Pharmaceutical Preparations": "制药",
    "Business Services": "商业服务", "Electronic Components": "电子元件", "Computer Manufacturing": "计算机硬件",
    "Finance: Consumer Services": "消费金融", "Major Banks": "大型银行", "Telecommunications Equipment": "通信设备",
    "Oil & Gas Production": "油气开采", "Real Estate Investment Trusts": "REITs",
    "Investment Bankers/Brokers/Service": "投行与券商", "EDP Services": "IT 服务",
    "Catalog/Specialty Distribution": "电商与专业分销",
    "Biotechnology: Biological Products (No Diagnostic Substances)": "生物制品", "Integrated oil Companies": "综合石油",
    "Department/Specialty Retail Stores": "百货与专业零售", "Medical Specialities": "专科医疗",
    "Electric Utilities: Central": "电力公用事业", "Electrical Products": "电气设备",
    "Consumer Electronics/Appliances": "消费电子", "Medical/Dental Instruments": "医疗器械",
    "Beverages (Production/Distribution)": "饮料", "Restaurants": "餐饮", "Aerospace": "航空航天",
    "Military/Government/Technical": "军工与政府服务", "Property-Casualty Insurers": "财产险",
    "Precious Metals": "贵金属", "Major Chemicals": "化工", "Investment Managers": "资产管理",
    "Marine Transportation": "航运", "Consumer Electronics/Video Chains": "消费电子零售",
    "Package Goods/Cosmetics": "日化与化妆品", "RETAIL: Building Materials": "建材零售",
    "Metal Fabrications": "金属加工", "Power Generation": "发电", "Computer Communications Equipment": "网络设备",
    "Radio And Television Broadcasting And Communications Equipment": "广播通信设备", "Metal Mining": "金属矿业",
    "Hotels/Resorts": "酒店度假", "Diversified Commercial Services": "综合商业服务",
    "Air Freight/Delivery Services": "航空货运与快递", "Natural Gas Distribution": "天然气分销",
    "Industrial Specialties": "工业专用品", "Hospital/Nursing Management": "医院与护理", "Commercial Banks": "商业银行",
    "Computer peripheral equipment": "计算机外设", "Steel/Iron Ore": "钢铁", "Specialty Insurers": "专业保险",
    "Engineering & Construction": "工程建筑", "Packaged Foods": "包装食品",
    "Services-Misc. Amusement & Recreation": "娱乐休闲", "Broadcasting": "广播电视",
    "Clothing/Shoe/Accessory Stores": "服饰零售", "Railroads": "铁路", "Homebuilding": "住宅建筑",
    "Construction/Ag Equipment/Trucks": "工程与农机", "Auto Parts:O.E.M.": "汽车零部件",
    "Retail-Auto Dealers and Gas Stations": "汽车经销与加油站", "Other Consumer Services": "其他消费服务",
    "Trucking Freight/Courier Services": "公路货运", "Transportation Services": "运输服务",
    "Biotechnology: Laboratory Analytical Instruments": "实验室仪器", "Life Insurance": "寿险",
    "Savings Institutions": "储蓄机构", "Oilfield Services/Equipment": "油服", "Water Supply": "水务",
}


def industry_zh(name):
    name = (name or "").strip()
    return INDUSTRY_ZH.get(name) or (name[:18] if name else "其他")

LEVERAGED, VOLATILITY, CRYPTO, COMMODITY, BOND, INTL, BROAD, SECTOR, OTHER = (
    "杠杆/反向", "波动率", "加密", "商品", "债券", "海外", "宽基", "行业/主题", "其他")
CATEGORY_ORDER = (BROAD, SECTOR, BOND, COMMODITY, CRYPTO, INTL, LEVERAGED, VOLATILITY, OTHER)
CORE_ETFS = {
    BROAD: "SPY VOO IVV VTI QQQ QQQM IWM DIA RSP SPLG SCHX ITOT MDY IJH IJR VTV VUG SCHD VIG VYM JEPI JEPQ "
           "IWF IWD SCHB SCHG MGK VO VB SPYG SPYV OEF",
    SECTOR: "XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC SMH SOXX IGV ARKK XBI IBB KRE KBE ITB XHB XOP OIH "
            "GDX GDXJ URA TAN ICLN LIT VGT VHT VNQ IYR XME COPX SILJ BOTZ CIBR HACK SKYY IHI ITA PPA",
    BOND: "TLT IEF SHY BIL SGOV AGG BND LQD HYG JNK TIP MUB VCIT VCSH BSV SHV EMB IEI GOVT USFR TLH VGLT BNDX",
    COMMODITY: "GLD IAU SLV USO UNG DBA DBC PDBC GLDM CPER PPLT SIVR BNO UCO SGOL",
    CRYPTO: "IBIT FBTC GBTC ARKB BITB HODL BTCO EZBC BRRR ETHA FETH ETHE BITO BTC ETH",
    LEVERAGED: "TQQQ SQQQ SOXL SOXS SPXL SPXS SPXU UPRO SSO SDS QLD QID TNA TZA LABU LABD TSLL TSLQ NVDL NVDU "
               "NVDX BITX ETHU CONL MSTU MSTX FAS FAZ TECL TECS YINN YANG BOIL KOLD NUGT DUST AGQ ZSL SH PSQ",
    VOLATILITY: "UVXY SVXY VXX VIXY VIXM UVIX SVIX",
    INTL: "EEM EFA VEA VWO IEFA IEMG FXI KWEB MCHI EWJ EWZ INDA EWT EWY EWG EWU ASHR VXUS ACWI EWC EWA",
}
CORE_CATEGORY = {t: c for c, tickers in CORE_ETFS.items() for t in tickers.split()}
CORE_ETF_LIST = list(CORE_CATEGORY)
_SHORT = r"\bSHORT\b(?![- ](?:TERM|DURATION|MATURITY|DATED))"
RULES = (
    (LEVERAGED, re.compile(r"\b(?:ULTRA\w*|LEVERAGED|INVERSE|BEAR|BULL|[123](?:\.\d+)?X|-[123]X)\b|" + _SHORT)),
    (CRYPTO, re.compile(r"\b(?:BITCOIN|ETHER|ETHEREUM|CRYPTO\w*|SOLANA|XRP|BLOCKCHAIN|DIGITAL ASSETS?|LITECOIN|"
                        r"DOGECOIN|AVALANCHE|CARDANO|POLKADOT|CHAINLINK)\b")),
    (COMMODITY, re.compile(r"\b(?:GOLD|SILVER|PLATINUM|PALLADIUM|PRECIOUS METALS?|OIL|CRUDE|NATURAL GAS|"
                           r"COMMODIT\w*|AGRICULTUR\w*|COPPER|BRENT|GASOLINE|CORN|WHEAT|SOYBEANS?)\b")),
    (BOND, re.compile(r"\b(?:TREASUR\w*|BONDS?|FIXED INCOME|MUNICIPAL|MUNI|HIGH YIELD|TIPS|AGGREGATE|MORTGAGE|"
                      r"T-BILLS?|BILLS?|FLOATING RATE|SENIOR LOAN|CLO|INVESTMENT GRADE|CORPORATE)\b")),
    (INTL, re.compile(r"\b(?:EMERGING|INTERNATIONAL|DEVELOPED|EX-U\.?S\.?|EAFE|CHINA|CHINESE|JAPAN|EUROPE\w*|"
                      r"INDIA|BRAZIL|KOREA|TAIWAN|MEXICO|CANADA|GERMANY|UNITED KINGDOM|AUSTRALIA|LATIN AMERICA|"
                      r"ASIA\w*|PACIFIC|AFRICA|SAUDI|ISRAEL|VIETNAM|INDONESIA|HONG KONG|SINGAPORE|SWITZERLAND|"
                      r"FRANCE|ITALY|SPAIN|ACWI|FRONTIER)\b")),
    (SECTOR, re.compile(r"\b(?:TECHNOLOGY|TECH|SEMICONDUCTORS?|FINANCIALS?|ENERGY|HEALTH ?CARE|BIOTECH\w*|"
                        r"REAL ESTATE|REITS?|UTILIT\w*|CONSUMER|INDUSTRIALS?|MATERIALS|COMMUNICATIONS?|CYBER\w*|"
                        r"ARTIFICIAL INTELLIGENCE|AI|ROBOTIC\w*|CLOUD|SOFTWARE|INTERNET|CLEAN|SOLAR|INNOVATION|"
                        r"GENOMIC\w*|AEROSPACE|DEFENSE|HOMEBUILDERS?|BANKS?|BANKING|INSURANCE|TRANSPORT\w*|RETAIL|"
                        r"MEDIA|GAMING|INFRASTRUCTURE|WATER|LITHIUM|BATTER\w*|ELECTRIC VEHICLES?|NUCLEAR|URANIUM|"
                        r"MINERS?|MINING|PHARMA\w*|MEDICAL|OIL SERVICES|EXPLORATION|METALS)\b")),
    (BROAD, re.compile(r"\b(?:S&P ?500|S&P|NASDAQ[- ]?100|DOW|RUSSELL|TOTAL (?:STOCK|MARKET|U\.?S\.?)|"
                       r"LARGE[- ]?CAP|MID[- ]?CAP|SMALL[- ]?CAP|EXTENDED MARKET|BROAD MARKET|EQUAL WEIGHT|"
                       r"DIVIDEND|VALUE|GROWTH|MOMENTUM|QUALITY|LOW VOLATILITY|MIN(?:IMUM)? VOL\w*|"
                       r"COVERED CALL|PREMIUM INCOME|BUFFER|CORE)\b")),
)
EQUITY_WORDS = re.compile(r"\b(?:MINERS?|MINING|SERVICES|EXPLORATION|PRODUCTION|PRODUCERS|EQUIT\w*|STOCKS?|"
                          r"ROYALT\w*|COMPANIES|DIVIDEND)\b")


def kind_of(subtype):
    if subtype in STOCK_SUBTYPES:
        return "stock"
    if subtype in FUND_SUBTYPES:
        return "etf"
    return None


def etf_category(ticker, name):
    """ETF category: hand-picked core list first, then ordered name rules (first match wins)."""
    if ticker in CORE_CATEGORY:
        return CORE_CATEGORY[ticker]
    text = (name or "").upper()
    if re.search(r"\bVIX\b|VOLATILITY (?:INDEX|FUTURES)", text) and not re.search(r"LOW VOLATILITY|MIN\w* VOL", text):
        return VOLATILITY
    for category, pattern in RULES:
        if not pattern.search(text):
            continue
        if category == COMMODITY and EQUITY_WORDS.search(text):
            return SECTOR       # gold miners, oil services ... hold equities, not the commodity
        if category == BOND and re.search(r"\b(?:EQUITY|DIVIDEND|COVERED CALL|PREMIUM)\b", text):
            continue
        return category
    return OTHER


# ---------- live ticks (Nasdaq watchlist) ----------

def parse_tick(row):
    """Normalize one watchlist row; None when the row has no usable price."""
    last = money(row.get("lastSalePrice"))
    if not last or last <= 0:
        return None
    status = STATUS.get(str(row.get("marketStatus") or "").strip().lower(), "closed")
    try:
        trade_ts = int(dt.datetime.fromisoformat(row["lastTradeTimestampDateTime"]).timestamp())
    except (KeyError, TypeError, ValueError):
        trade_ts = None
    return {"ticker": (row.get("symbol") or "").strip().upper(), "status": status, "last": last,
            "chg_abs": money(row.get("netChange")), "chg_pct": money(row.get("percentageChange")),
            "volume": money(row.get("volume")) or 0.0, "prev_close": fnum(row.get("previousClosePrice")),
            "open": fnum(row.get("openingPrice")), "trade_ts": trade_ts}


def session_view(tick, semantics="unverified", last_open=None):
    """Session price/change/volume a tick describes (DESIGN C2).

    open          -> live session: price = last, change = percentageChange, cumulative volume.
    pre           -> last completed session (verified 2026-10-28): close = last - netChange,
                     change = close / previousClose - 1, volume = that session's volume,
                     pre-market move = percentageChange.
    after/closed  -> same derivation, which is only *assumed* until the after-hours self-check
                     passes (semantics='validated'); 'failed' falls back to the last live tick.
    """
    status = tick["status"]
    if status == "open":
        return {"price": tick["last"], "change": tick["chg_pct"], "volume": tick["volume"],
                "turnover": tick["volume"] * tick["last"], "ext": None, "live": True, "unverified": False}
    if status in ("after", "closed") and semantics == "failed":
        if last_open:
            return dict(session_view(dict(last_open, status="open")), live=False)
        return None
    if tick["chg_abs"] is None:
        return None
    close = tick["last"] - tick["chg_abs"]
    if close <= 0:
        return None
    prev = tick["prev_close"]
    change = (close / prev - 1) * 100 if prev else None
    ext = tick["chg_pct"] if status in ("pre", "after") and tick["chg_abs"] else None
    return {"price": close, "change": change, "volume": tick["volume"], "turnover": tick["volume"] * close,
            "ext": ext, "live": False, "ext_price": tick["last"] if ext is not None else None,
            "unverified": status in ("after", "closed") and semantics != "validated"}


def elapsed_fraction(now, open_at):
    minutes = (now - open_at) / 60
    return max(0.02, min(1.0, minutes / SESSION_MINUTES))


def expected_volume(avg_volume, live, fraction):
    """Volume expected so far: front-loaded intraday profile f(x) = x^0.7 (approximation)."""
    return avg_volume * (fraction ** 0.7 if live else 1.0)


def board(ticks, meta, baselines, now, open_at=None, semantics="unverified", last_open=None):
    """Rows for every universe ticker with a usable session view."""
    rows = []
    last_open = last_open or {}
    fraction = elapsed_fraction(now, open_at) if open_at else 1.0
    for ticker, tick in ticks.items():
        info = meta.get(ticker) or {}
        kind = info.get("kind")
        if kind not in ("stock", "etf"):
            continue
        view = session_view(tick, semantics, last_open.get(ticker))
        if not view or view["turnover"] <= 0 or view["change"] is None:
            continue
        avg = baselines.get(ticker)
        rvol = view["volume"] / expected_volume(avg, view["live"], fraction) if avg else None
        sector = SECTOR_ZH.get(info.get("sector") or "", info.get("sector") or "其他") if kind == "stock" else None
        rows.append({"symbol": ticker, "kind": kind, "name": info.get("name") or "", "sector": sector,
                     "industry": industry_zh(info.get("industry")) if kind == "stock" else None,
                     "category": info.get("category") or OTHER,
                     "price": view["price"], "change": view["change"], "volume": view["volume"],
                     "turnover": view["turnover"], "ext": view["ext"], "ext_price": view.get("ext_price"),
                     "live": view["live"], "unverified": view["unverified"], "rvol": rvol,
                     "flow": flow_value(view["turnover"], view["change"])})
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows


def summary(rows):
    up = [r for r in rows if r["change"] > 0]
    down = [r for r in rows if r["change"] < 0]
    total = sum(r["turnover"] for r in rows)
    return {"count": len(rows), "turnover": total, "flow": sum(r["flow"] for r in rows),
            "advancers": len(up), "decliners": len(down),
            "up_turnover": sum(r["turnover"] for r in up), "down_turnover": sum(r["turnover"] for r in down),
            "wchange": sum(r["turnover"] * r["change"] for r in rows) / total if total else None}


def group_flows(rows, key, minimum=1):
    groups = {}
    for r in rows:
        name = r.get(key) or "其他"
        g = groups.setdefault(name, {"name": name, "flow": 0.0, "turnover": 0.0, "weighted": 0.0, "count": 0})
        g["flow"] += r["flow"]
        g["turnover"] += r["turnover"]
        g["weighted"] += r["turnover"] * r["change"]
        g["count"] += 1
    out = [dict(g, change=g["weighted"] / g["turnover"] if g["turnover"] else 0.0)
           for g in groups.values() if g["count"] >= minimum]
    return sorted(out, key=lambda g: g["flow"], reverse=True)


def leaders(rows, n=5):
    ranked = sorted(rows, key=lambda r: r["flow"], reverse=True)
    return [r for r in ranked if r["flow"] > 0][:n], [r for r in reversed(ranked) if r["flow"] < 0][:n]


def equity_overview(rows, top_n=20):
    stocks = [r for r in rows if r["kind"] == "stock"]
    etfs = [r for r in rows if r["kind"] == "etf"]
    plain_etfs = [r for r in etfs if r["category"] not in (LEVERAGED, VOLATILITY)]
    inflow, outflow = leaders(stocks)
    etf_in, etf_out = leaders(plain_etfs, 3)
    return {"stocks": summary(stocks), "etfs": summary(etfs), "top": stocks[:top_n], "etf_top": etfs[:10],
            "inflow": inflow, "outflow": outflow, "etf_in": etf_in, "etf_out": etf_out,
            "sectors": group_flows(stocks, "sector"), "industries": group_flows(stocks, "industry", minimum=3),
            "categories": group_flows(etfs, "category"),
            "gainers": sorted((r for r in stocks if r["turnover"] >= 5e7 and r["change"] > 0),
                              key=lambda r: r["change"], reverse=True)[:5],
            "losers": sorted((r for r in stocks if r["turnover"] >= 5e7 and r["change"] < 0),
                             key=lambda r: r["change"])[:5]}


# ---------- anomalies ----------

def _anomaly(kind, symbol, typ, direction, severity, major, text, day, text_en=None):
    return {"key": f"{typ}:{symbol}:{'up' if direction > 0 else 'down'}:{day}", "kind": kind, "symbol": symbol,
            "type": typ, "direction": direction, "severity": severity, "major": major, "text": text,
            "text_en": text_en or text}


def equity_anomalies(rows, now, past=None, day=None):
    """Price, volume, intraday-velocity and extended-hours anomalies (thresholds: DESIGN §5).

    severity = multiple of the type's threshold (volume capped at 3) so types rank together.

    past: {minutes_ago: {ticker: price}} from earlier live ticks (15 and 60 minute lookbacks).
    """
    day = day or dt.datetime.fromtimestamp(now, ET).date().isoformat()
    past = past or {}
    found = []
    for r in rows:
        etf = r["kind"] == "etf"
        if etf and r["category"] in (LEVERAGED, VOLATILITY):
            continue
        sym, chg, turnover = r["symbol"], r["change"], r["turnover"]
        label = "ETF " if etf else ""
        move_limit, rvol_limit = (3.0, 3.0) if etf else (7.0, 2.5)
        # A completed session's movers are listed but never pushed: by then the news is stale.
        if turnover >= 5e7 and abs(chg) >= move_limit:
            major = r["live"] and abs(chg) >= (6.0 if etf else 12.0) and turnover >= 2e8
            found.append(_anomaly(r["kind"], sym, "move", chg, abs(chg) / move_limit, major,
                                  f"{label}{sym} {'大涨' if chg > 0 else '大跌'} {chg:+.1f}%", day,
                                  f"{label}{sym} {'surged' if chg > 0 else 'slumped'} {chg:+.1f}%"))
        if r["rvol"] and turnover >= 5e7 and r["rvol"] >= rvol_limit:
            major = r["live"] and r["rvol"] >= 5 and abs(chg) >= 5 and turnover >= 2e8
            found.append(_anomaly(r["kind"], sym, "volume", chg or 1, min(r["rvol"] / rvol_limit, 3.0), major,
                                  f"{label}{sym} 放量 {r['rvol']:.1f} 倍（{chg:+.1f}%）", day,
                                  f"{label}{sym} volume {r['rvol']:.1f}x normal ({chg:+.1f}%)"))
        if r["live"] and turnover >= 3e7:
            for minutes, limit in ((15, 2.5), (60, 4.0)):
                before = (past.get(minutes) or {}).get(sym)
                if before:
                    move = (r["price"] / before - 1) * 100
                    if abs(move) >= limit:
                        major = abs(move) >= limit * 1.6 and turnover >= 1e8
                        found.append(_anomaly(r["kind"], sym, f"velocity{minutes}", move, abs(move) / limit * 1.2, major,
                                              f"{label}{sym} {minutes} 分钟{'急拉' if move > 0 else '急跌'} {move:+.1f}%",
                                              day, f"{label}{sym} {'spiked' if move > 0 else 'dropped'} "
                                                   f"{move:+.1f}% in {minutes} min"))
                        break
        if r["ext"] is not None and turnover >= 5e7 and abs(r["ext"]) >= 5:
            major = abs(r["ext"]) >= 10 and turnover >= 2e8
            found.append(_anomaly(r["kind"], sym, "extended", r["ext"], abs(r["ext"]) / 5, major,
                                  f"{label}{sym} 盘外 {r['ext']:+.1f}%", day,
                                  f"{label}{sym} extended hours {r['ext']:+.1f}%"))
    return sorted(found, key=lambda a: a["severity"], reverse=True)


# ---------- universe ----------

def select_universe(meta, tradable, n_stocks=500, n_etfs=200):
    """Top stocks/ETFs by reference turnover among Coinbase-tradable tickers, plus core ETFs."""
    stocks = sorted((m for t, m in meta.items() if t in tradable and m.get("kind") == "stock"
                     and (m.get("ref_turnover") or 0) > 0), key=lambda m: m["ref_turnover"], reverse=True)[:n_stocks]
    etfs = sorted((m for t, m in meta.items() if t in tradable and m.get("kind") == "etf"
                   and (m.get("ref_turnover") or 0) > 0), key=lambda m: m["ref_turnover"], reverse=True)[:n_etfs]
    chosen = {m["ticker"] for m in stocks + etfs}
    core = [{"ticker": t, "kind": "etf"} for t in CORE_ETF_LIST if t in tradable and t not in chosen]
    return [{"ticker": m["ticker"], "kind": m["kind"]} for m in stocks + etfs] + core


def _session_times(info):
    try:
        return (dt.datetime.fromisoformat(info["openRaw"]).replace(tzinfo=ET),
                dt.datetime.fromisoformat(info["closeRaw"]).replace(tzinfo=ET))
    except (KeyError, TypeError, ValueError):
        return None, None


def _previous_trade_date(info):
    try:
        return dt.datetime.strptime(info.get("previousTradeDate") or "", "%b %d, %Y").date().isoformat()
    except ValueError:
        return None


def completed_session_date(info, now):
    """Date of the most recent completed regular session per Nasdaq market-info."""
    if not info:
        return None
    open_at, close_at = _session_times(info)
    if not open_at:
        return None
    return open_at.date().isoformat() if dt.datetime.fromtimestamp(now, ET) >= close_at else _previous_trade_date(info)


def session_info(info, ticks, now, semantics="unverified"):
    """Which session the board shows: phase from the ticks' own status, date from market-info."""
    counts = {}
    for t in ticks.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    phase = max(counts, key=counts.get) if counts else "closed"
    now_et = dt.datetime.fromtimestamp(now, ET)
    open_at, _ = _session_times(info or {})
    if phase in ("open", "after") and open_at:
        date = open_at.date().isoformat()
    else:
        date = completed_session_date(info, now)
    return {"phase": phase, "date": date, "as_of": now_et.strftime("%H:%M") if phase == "open" else None,
            "open_at": int(open_at.timestamp()) if open_at and phase == "open" else None,
            "unverified": phase in ("after", "closed") and semantics != "validated"}


def extended_hours(info, now):
    """True between pre-market open and after-hours close (04:00-20:00 ET) on a session day."""
    try:
        start = dt.datetime.fromisoformat(info["pmOpenRaw"]).replace(tzinfo=ET)
        end = dt.datetime.fromisoformat(info["ahCloseRaw"]).replace(tzinfo=ET)
    except (KeyError, TypeError, ValueError):
        return True     # unknown: keep polling at the fast cadence
    return start <= dt.datetime.fromtimestamp(now, ET) < end


def daily_bar(ticker, tick, session_date, semantics="unverified"):
    """Daily bar for the last completed session; only from verified derivations (DESIGN C2)."""
    if tick["status"] == "open" or (tick["status"] != "pre" and semantics != "validated"):
        return None
    view = session_view(tick, semantics)
    if not view or not view["volume"]:
        return None
    return {"ticker": ticker, "date": session_date, "close": view["price"], "change": view["change"],
            "volume": view["volume"], "turnover": view["turnover"], "source": "watchlist"}


def history_bars(ticker, rows):
    out = []
    for r in rows:
        try:
            date = dt.datetime.strptime(r.get("date") or "", "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        close, volume = money(r.get("close")), money(r.get("volume"))
        if close and volume:
            out.append({"ticker": ticker, "date": date, "close": close, "change": None, "volume": volume,
                        "turnover": close * volume, "source": "nasdaq"})
    return out


def after_hours_check(after_ticks, open_ticks, minimum=20):
    """Hypothesis test for DESIGN C2 on after-hours/closed rows.

    If netChange is measured from today's close (pre-market style), last - netChange equals
    today's close (~ the session's last live price); otherwise it equals yesterday's close.
    Counts which hypothesis each liquid ticker supports; ok when >= 70% are pre-market style.
    """
    wins = samples = 0
    diffs = []
    for ticker, tick in after_ticks.items():
        live = open_ticks.get(ticker)
        if not live or tick["chg_abs"] is None or not live.get("last") or not live.get("chg_pct"):
            continue
        derived = tick["last"] - tick["chg_abs"]
        today = live["last"]
        yesterday = today / (1 + live["chg_pct"] / 100)
        if abs(today - yesterday) / today < 0.002:      # too flat to tell the hypotheses apart
            continue
        samples += 1
        wins += abs(derived - today) < abs(derived - yesterday)
        diffs.append(abs(derived / today - 1) * 100)
    if samples < minimum:
        return None
    diffs.sort()
    share = wins / samples
    return {"ok": share >= 0.7, "pre_like_share": round(share, 3),
            "median_diff_pct": round(diffs[len(diffs) // 2], 3), "samples": samples}
