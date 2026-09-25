"""Protocol tests for the heyjev-fm helper. No inference by default.

Default run validates JSON-lines protocol and error paths only (models
shape, unknown-model ask -> unavailable, quit/EOF). Real model calls are
opt-in integration tests: HEYJEV_FM_LIVE=1 runs one on_device ask and one
private_cloud ask, reporting status without asserting availability. A
skipped live test is not a verified backend.

Skips entirely if bin/heyjev-fm is missing (helper is optional).
"""
import json
import os
import subprocess
import unittest

BIN = os.path.join(os.path.dirname(__file__), "bin", "heyjev-fm")
LIVE = os.environ.get("HEYJEV_FM_LIVE") == "1"


def run_helper(requests, timeout=120):
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [BIN], input=payload, capture_output=True, text=True, timeout=timeout
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


@unittest.skipIf(not os.path.exists(BIN), "heyjev-fm binary missing (optional helper)")
class HeyjevFmProtocolTest(unittest.TestCase):
    def test_models_shape(self):
        (resp,) = run_helper([{"op": "models"}, {"op": "quit"}])[:1]
        self.assertEqual(resp["op"], "models")
        self.assertIsInstance(resp["models"], list)
        self.assertGreaterEqual(len(resp), 1)
        by_id = {m["id"]: m for m in resp["models"]}
        self.assertIn("on_device", by_id)
        od = by_id["on_device"]
        self.assertFalse(od["remote"])
        self.assertIsInstance(od["available"], bool)
        if not od["available"]:
            self.assertTrue(od["reason"])
        if "private_cloud" in by_id:
            pc = by_id["private_cloud"]
            self.assertTrue(pc["remote"])
            # PCC must stay unavailable in this unsigned build.
            self.assertFalse(pc["available"], "PCC must not report available without entitlement")
            self.assertTrue(pc["reason"])

    def test_unknown_model_ask_is_unavailable(self):
        resps = run_helper(
            [
                {"op": "models"},
                {
                    "op": "ask",
                    "id": "q1",
                    "model": "private_cloud",
                    "instructions": "",
                    "history": [],
                    "prompt": "Say hi.",
                    "max_tokens": 32,
                },
                {"op": "quit"},
            ]
        )
        asks = [r for r in resps if r.get("op") == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["id"], "q1")
        self.assertEqual(asks[0]["status"], "unavailable")
        self.assertTrue(asks[0]["error"])


@unittest.skipIf(not os.path.exists(BIN), "heyjev-fm binary missing (optional helper)")
@unittest.skipIf(not LIVE, "live inference opt-in only (HEYJEV_FM_LIVE=1)")
class HeyjevFmLiveTest(unittest.TestCase):
    def test_live_asks_report_only(self):
        resps = run_helper(
            [
                {"op": "models"},
                {
                    "op": "ask",
                    "id": "live-od",
                    "model": "on_device",
                    "instructions": "Reply in one short sentence.",
                    "history": [],
                    "prompt": "Say hello.",
                    "max_tokens": 64,
                },
                {
                    "op": "ask",
                    "id": "live-pcc",
                    "model": "private_cloud",
                    "instructions": "",
                    "history": [],
                    "prompt": "Say hello.",
                    "max_tokens": 64,
                },
                {"op": "quit"},
            ],
            timeout=300,
        )
        print("\nheyjev-fm live results:")
        for r in resps:
            print(json.dumps(r)[:400])


if __name__ == "__main__":
    unittest.main()
