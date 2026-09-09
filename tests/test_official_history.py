import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from backend.history_store import HistoryStore
from backend.official_history_sync import parse_archive
from backend.corporate_actions_sync import parse_subject
from backend.ml_pipeline import feature_rows
from backend.ml.calibration import apply_calibration, calibration_metrics, fit_isotonic


HEADER = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol,OpnIntrst\n"


class OfficialHistoryTests(unittest.TestCase):
    def test_isotonic_calibration_is_serializable_and_improves_brier(self):
        raw=[.05,.10,.15,.20,.75,.80,.85,.90]*20
        labels=[0,0,0,1,0,1,1,1]*20
        calibration=fit_isotonic(raw,labels)
        calibrated=[apply_calibration(value,calibration) for value in raw]
        self.assertEqual(calibration["method"],"isotonic")
        self.assertLessEqual(calibration_metrics(labels,calibrated)["brier_score"],calibration_metrics(labels,raw)["brier_score"])
        self.assertTrue(all(0 <= value <= 1 for value in calibrated))
    def test_corporate_action_subject_parser_handles_adjustable_events(self):
        bonus=parse_subject("Bonus 1:2")
        self.assertEqual((bonus["ratio_from"],bonus["ratio_to"]),(2,3))
        split=parse_subject("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
        self.assertEqual((split["ratio_from"],split["ratio_to"]),(1,10))
        self.assertEqual(parse_subject("Dividend - Rs 5 Per Share & Special Dividend Rs 3 Per Share")["cash_amount"],8)
        self.assertIsNone(parse_subject("Buy Back"))

    def test_v2_features_use_regime_context_and_triple_barriers(self):
        bars=[]
        for index in range(70):
            close=100+index*.2
            bars.append({"timestamp":f"2025-01-{1+index:02d}","open":close-.1,"high":close+1.5,"low":close-.5,"close":close,"volume":100000+index*100})
        context={row["timestamp"]:{"market_return_1d":.001,"market_return_20d":.02,"market_breadth":.6,"market_volatility_20d":.01} for row in bars}
        rows=feature_rows("NSE","TEST",bars,market_context=context,feature_set="daily_v2")
        self.assertIn("relative_strength_20d",rows[0]["features"])
        self.assertAlmostEqual(rows[0]["features"]["market_breadth"],.1)
        self.assertIn(rows[0]["label"],{0,1,None})

    def test_udiff_parser_filters_nse_to_eq_and_supports_bse_groups(self):
        day = date(2025, 6, 27)
        nse = (HEADER +
               "2025-06-27,2025-06-27,CM,NSE,STK,2885,INE001A01036,RELIANCE,EQ,1500,1520,1490,1510,10000,0\n" +
               "2025-06-27,2025-06-27,CM,NSE,STK,1,TEST,NOTSTOCK,GB,100,101,99,100,1,0\n").encode()
        rows = parse_archive("NSE", day, nse)
        self.assertEqual([row["symbol"] for row in rows], ["RELIANCE"])
        self.assertEqual(rows[0]["volume"], 10000)

        bse = (HEADER + "2025-06-27,2025-06-27,CM,BSE,STK,500325,INE009A01021,RELIANCE,A,1500,1520,1490,1510,8000,0\n").encode()
        self.assertEqual(parse_archive("BSE", day, bse)[0]["instrument_token"], 500325)

    def test_zip_parser_and_archive_provenance_are_auditable(self):
        csv_payload = (HEADER + "2025-06-27,2025-06-27,CM,NSE,STK,2885,INE001A01036,RELIANCE,EQ,1500,1520,1490,1510,10000,0\n").encode()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("bhav.csv", csv_payload)
        rows = parse_archive("NSE", date(2025, 6, 27), payload.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            store = HistoryStore(Path(folder) / "history.db")
            store.save_archive("NSE", "2025-06-27", rows, "official_exchange_bhavcopy",
                               "https://example.test/bhav.zip", "abc", len(payload.getvalue()), "now")
            self.assertTrue(store.archive_imported("NSE", "2025-06-27", "official_exchange_bhavcopy"))
            self.assertEqual(store.rebuild_coverage("NSE", updated_at="now"), 1)
            self.assertEqual(store.stats()["bars"], 1)


if __name__ == "__main__":
    unittest.main()
