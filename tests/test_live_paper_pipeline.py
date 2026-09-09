import os
import struct
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.kite_stream import KiteStream, parse_binary
from backend.live_inference import LivePaperInference, build_causal_feature_vector
from backend.live_stream_service import LiveStreamService
from backend.ml_pipeline import FEATURES_V2
from backend.ml.trading_policy import classify_regime, rank_candidates, score_candidate
from backend.upstox_backfill import aggregate_5minute, parse_upstox_candles
from backend.upstox_stream import UpstoxStream, decode_feed_response, provider_token


def _varint(value):
    output=bytearray()
    while value>0x7F:
        output.append((value&0x7F)|0x80); value >>= 7
    output.append(value)
    return bytes(output)


def _field(field,wire,value):
    return _varint((field<<3)|wire)+value


def _len_field(field,payload):
    return _field(field,2,_varint(len(payload))+payload)


def _double_field(field,value):
    return _field(field,1,struct.pack("<d",value))


class LivePaperPipelineTests(unittest.TestCase):
    def _daily(self,count=60):
        start=datetime(2026,1,1,tzinfo=timezone.utc); rows=[]; close=100.0
        for index in range(count):
            close*=1.001 if index%3 else .999
            rows.append({"timestamp":start+timedelta(days=index),"open":close-.2,"high":close+.8,"low":close-.7,
                         "close":close,"volume":1_000_000+index*1000,"oi":0})
        return rows

    def test_training_compatible_features_are_causal_and_complete(self):
        daily=self._daily(); watermark=daily[-1]["timestamp"]+timedelta(days=1,hours=5)
        session={"timestamp":watermark,"open":daily[-1]["close"]+.1,"high":daily[-1]["close"]+1,
                 "low":daily[-1]["close"]-.5,"close":daily[-1]["close"]+.6,"volume":240000,"oi":0}
        context={"market_return_1d":.002,"market_return_20d":.03,"market_breadth":.61,"market_volatility_20d":.012}
        features=build_causal_feature_vector("NSE","TEST",daily,session,context)
        self.assertTrue(set(FEATURES_V2).issubset(features))
        self.assertEqual(features["market_return_1d"],.002)
        with self.assertRaisesRegex(ValueError,"precede"):
            build_causal_feature_vector("NSE","TEST",daily+[{**daily[-1],"timestamp":watermark}],session,context)

    def test_index_full_packet_decodes_timestamp_and_signed_change(self):
        token=256265; divisor=100
        packet=struct.pack(">IIIIIIiI",token,2212345,2220000,2200000,2205000,2210000,-1250,1780000000)
        frame=struct.pack(">HH",1,len(packet))+packet
        tick=parse_binary(frame)[0]
        self.assertEqual(tick["instrument_token"],token)
        self.assertEqual(tick["last_price"],22123.45)
        self.assertEqual(tick["change"],-12.5)
        self.assertEqual(tick["exchange_timestamp"],1780000000)

    def test_mixed_subscription_modes_never_exceed_kite_limit(self):
        stream=KiteStream("key","token",lambda ticks:None)
        stream.subscribe(range(1,2501),"full")
        stream.subscribe(range(2501,4001),"quote")
        self.assertEqual(len(stream.tokens),3000)
        self.assertEqual(len(stream.mode_tokens["full"]),2500)
        self.assertEqual(len(stream.mode_tokens["quote"]),500)

    def test_upstox_feed_v3_ltp_protobuf_decodes_to_internal_tick(self):
        instrument_key="NSE_INDEX|India VIX"
        ltpc=_double_field(1,14.25)+_field(2,0,_varint(1780000000000))+_field(3,0,_varint(1))+_double_field(4,14.0)
        feed=_len_field(1,ltpc)
        entry=_len_field(1,instrument_key.encode())+_len_field(2,feed)
        response=_field(1,0,_varint(1))+_len_field(2,entry)+_field(3,0,_varint(1780000000500))
        ticks=decode_feed_response(response)
        self.assertEqual(len(ticks),1)
        self.assertEqual(ticks[0]["instrument_key"],instrument_key)
        self.assertEqual(ticks[0]["instrument_token"],provider_token(instrument_key))
        self.assertEqual(ticks[0]["last_price"],14.25)
        self.assertEqual(ticks[0]["exchange_timestamp"],1780000000.0)

    def test_upstox_stream_sends_binary_subscription_payload(self):
        sent=[]
        class DummyWs:
            def send(self,payload,**kwargs):
                sent.append((payload,kwargs))
        stream=UpstoxStream("token",lambda ticks:None)
        stream.subscribe(["NSE_EQ|INE002A01018"],"quote")
        stream._send_subscriptions(DummyWs(),stream.mode_keys)
        payload,kwargs=sent[0]
        self.assertIsInstance(payload,bytes)
        self.assertIn(b'"mode": "ltpc"',payload)
        self.assertIn(b"NSE_EQ|INE002A01018",payload)
        self.assertIn("opcode",kwargs)

    def test_upstox_rest_candles_parse_and_aggregate_to_5minute(self):
        payload={"data":{"candles":[
            ["2026-07-14T09:16:00+05:30",101,103,100,102,20,0],
            ["2026-07-14T09:15:00+05:30",100,102,99,101,10,0],
            ["2026-07-14T09:20:00+05:30",104,105,103,104.5,30,0],
        ]}}
        candles=parse_upstox_candles(payload)
        self.assertEqual([float(item["close"]) for item in candles],[101.0,102.0,104.5])
        bars=aggregate_5minute(candles)
        self.assertEqual(len(bars),2)
        self.assertEqual(float(bars[0]["open"]),100.0)
        self.assertEqual(float(bars[0]["high"]),103.0)
        self.assertEqual(float(bars[0]["low"]),99.0)
        self.assertEqual(float(bars[0]["close"]),102.0)
        self.assertEqual(bars[0]["volume"],30)

    def test_live_paper_engine_refuses_unlocked_execution_fuses(self):
        with patch.dict(os.environ,{"LIVE_ELIGIBLE":"TRUE","NIVESH_LIVE_TRADING_ENABLED":"0"}):
            with self.assertRaisesRegex(RuntimeError,"execution fuses locked"):
                LivePaperInference("postgresql://unused","redis://unused")

    def test_read_only_stream_fails_before_connecting_without_credentials(self):
        environment={"DATABASE_URL":"postgresql://unused","REDIS_URL":"redis://unused","KITE_API_KEY":"","KITE_ACCESS_TOKEN":"",
                     "NIVESH_MARKET_DATA_PROVIDER":"zerodha","LIVE_ELIGIBLE":"FALSE","NIVESH_LIVE_TRADING_ENABLED":"0"}
        with patch.dict(os.environ,environment,clear=False):
            with self.assertRaisesRegex(RuntimeError,"today's supported access token"):
                LiveStreamService()

    def test_read_only_upstox_stream_fails_before_connecting_without_token(self):
        environment={"DATABASE_URL":"postgresql://unused","REDIS_URL":"redis://unused","NIVESH_MARKET_DATA_PROVIDER":"upstox",
                     "UPSTOX_ACCESS_TOKEN":"","UPSTOX_ANALYTICS_TOKEN":"","LIVE_ELIGIBLE":"FALSE","NIVESH_LIVE_TRADING_ENABLED":"0"}
        with patch.dict(os.environ,environment,clear=False):
            with self.assertRaisesRegex(RuntimeError,"Upstox stream requires"):
                LiveStreamService()

    def test_v36_schema_has_idempotent_feature_and_prediction_contract(self):
        sql=Path("migrations/postgres/v3_6_live_paper_pipeline.sql").read_text()
        self.assertIn("PRIMARY KEY(model_version,instrument_id,timeframe,observed_at)",sql)
        self.assertIn("feature_hash",sql)
        self.assertIn("market_data_gaps",sql)

    def test_v37_release_schema_keeps_preflight_non_executable(self):
        sql=Path("migrations/postgres/v3_7_release_operations.sql").read_text()
        self.assertIn("operational_preflight_runs",sql)
        self.assertIn("CHECK(NOT orders_allowed)",sql)
        self.assertIn("model_release_manifests",sql)

    def test_cost_aware_policy_rejects_weak_edge_and_ranks_net_edge(self):
        features={name:0.0 for name in FEATURES_V2}
        features.update({"market_return_20d":.04,"market_breadth":.2,"atr_14":.02,"volatility_20d":.015})
        row={"features":features}
        weak=score_candidate(row,.53,.54,20)
        strong=score_candidate(row,.66,.68,20)
        expensive=score_candidate(row,.66,.68,150)
        self.assertEqual(classify_regime(features),"bull")
        self.assertFalse(weak.accepted)
        self.assertTrue(strong.accepted)
        self.assertGreater(strong.score,expensive.score)
        self.assertEqual(rank_candidates([weak,expensive,strong],1),[strong])

    def test_v38_schema_requires_real_cost_and_broker_shadow_evidence(self):
        sql=Path("migrations/postgres/v3_8_model_policy_evidence.sql").read_text()
        self.assertIn("ZERODHA_CONTRACT_NOTE",sql)
        self.assertIn("BROKER_CONNECTED_SHADOW",sql)
        self.assertIn("chk_real_cost_evidence",sql)

    def test_v39_schema_keeps_provider_keys_separate_from_broker_tokens(self):
        sql=Path("migrations/postgres/v3_9_market_data_providers.sql").read_text()
        self.assertIn("instrument_provider_keys",sql)
        self.assertIn("provider_key",sql)
        self.assertIn("UNIQUE(provider, provider_token)",sql)


if __name__=="__main__": unittest.main()
