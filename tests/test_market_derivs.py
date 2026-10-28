import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_derivs as d

NOW = 1_793_174_400
BIP = {"product_id": "BIP-20DEC30-CDE", "price": "81115", "price_percentage_change_24h": "1.5",
       "approximate_quote_24h_volume": "249310197.1",
       "future_product_details": {"contract_code": "BIP", "contract_root_unit": "BTC", "display_name": "Bitcoin Perpetual",
                                  "futures_asset_type": "FUTURES_ASSET_TYPE_CRYPTO", "contract_size": "0.01",
                                  "open_interest": "186914", "funding_rate": "0.000002", "funding_interval": "3600s",
                                  "index_price": "81100.528257", "contract_expiry": "2089-12-30T16:00:00Z"}}


def product(pid, code, root, price, expiry, oi="100", kind="CRYPTO", perp=False, funding="", chg="1"):
    return {"product_id": pid, "price": price, "price_percentage_change_24h": chg,
            "future_product_details": {"contract_code": code, "contract_root_unit": root,
                                       "display_name": ("X Perpetual" if perp else "X Futures"),
                                       "futures_asset_type": "FUTURES_ASSET_TYPE_" + kind, "contract_size": "1",
                                       "open_interest": oi, "funding_rate": funding,
                                       "funding_interval": "3600s" if perp else "", "contract_expiry": expiry}}


class Derivs(unittest.TestCase):
    def test_parse_real_perp(self):
        c = d.parse_contract(BIP)
        self.assertTrue(c["perp"])
        self.assertAlmostEqual(c["oi_usd"], 186914 * 0.01 * 81115)
        self.assertAlmostEqual(d.annualized_funding(c), 0.000002 * 24 * 365 * 100)
        self.assertAlmostEqual(d.premium(c), (81115 / 81100.528257 - 1) * 100)
        self.assertIsNone(d.parse_contract({"product_id": "X", "price": ""}))

    def test_perps_oi_change_and_term_structure(self):
        contracts = d.parse_contracts([BIP, product("BIT-27NOV26-CDE", "BIT", "BTC", "82000", "2026-11-27T16:00:00Z")])
        perps = d.crypto_perps(contracts, past_4h={"BIP-20DEC30-CDE": {"oi_contracts": 170000.0}})
        self.assertAlmostEqual(perps[0]["oi_chg4"], (186914 / 170000 - 1) * 100)
        self.assertIsNone(perps[0]["oi_chg24"])
        term = d.term_structure(contracts, {"BTC": 81000.0}, NOW)
        row = term["BTC"][0]
        self.assertAlmostEqual(row["annualized"], (82000 / 81000 - 1) * 100 * 365 / row["days"])

    def test_macro_board_prefers_perp_then_open_interest_then_nearest(self):
        contracts = d.parse_contracts([
            product("US5-19DEC30-CDE", "US5", "CDEUS5", "3120", "2030-12-19T16:00:00Z", kind="STOCKS", perp=True, funding="-0.00006"),
            product("NOL-19NOV26-CDE", "NOL", "CDEOIL", "101", "2026-11-19T16:00:00Z", kind="ENERGY"),
            product("NOL-18DEC26-CDE", "NOL", "CDEOIL", "99", "2026-12-18T16:00:00Z", kind="ENERGY"),
            product("NOL-19JAN27-CDE", "NOL", "CDEOIL", "98", "2027-01-19T16:00:00Z", oi="0", kind="ENERGY")])
        board = {m["code"]: m for m in d.macro_board(contracts)}
        self.assertEqual(board["NOL"]["product_id"], "NOL-19NOV26-CDE")
        self.assertTrue(board["US5"]["perp"])
        self.assertEqual([m["code"] for m in d.macro_board(contracts)], ["US5", "NOL"])

    def test_anomalies(self):
        perps = [{"asset": "BTC", "oi_usd": 2e8, "funding_ann": 75.0, "oi_chg4": 20.0, "premium": 0.1}]
        macro = [{"code": "GOL", "label": "黄金", "kind": "commodity", "chg24": -7.0}]
        found = {a["type"]: a for a in d.deriv_anomalies(perps, macro, NOW)}
        self.assertTrue(found["funding"]["major"])
        self.assertIn("多头拥挤", found["funding"]["text"])
        self.assertFalse(found["oi"]["major"])
        self.assertTrue(found["macro"]["major"])
        self.assertNotIn("premium", found)


if __name__ == "__main__":
    unittest.main()
