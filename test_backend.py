import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent / "model"))
import main
from db_service import DBService


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.addCleanup(main.app.dependency_overrides.clear)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.legacy = Path(directory.name) / "calls.json"
        self.db = DBService(self.legacy)
        self.enterContext(patch.object(main, "database", return_value=self.db))

    def login(self, uid="one", admin=False):
        main.app.dependency_overrides[main.identity] = lambda: {"sub": uid, "admin": admin}

    def test_access(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/insights").status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/update-sop", json={}).status_code, 403)
        self.assertNotEqual(main.policy_key({"sub":"one"}, "same"), main.policy_key({"sub":"two"}, "same"))

    def test_suggestion(self):
        self.login()
        suggestion = {"intent": "Ask for a name", "suggestion": "What is your name?"}
        with patch.object(main.sop_converter, "generate_sop_suggestion", return_value=suggestion):
            self.assertEqual(self.client.post("/generate-sop-suggestion", json={"text":"ask name"}).json(), suggestion)

    def test_chat(self):
        self.login()
        import httpx
        from unittest.mock import AsyncMock
        response = httpx.Response(200, request=httpx.Request("POST", main.sop_converter.api_url),
                                  json={"choices":[{"message":{"content":"Welcome"}}]})
        outbound = AsyncMock(return_value=response)
        with patch.object(main.sop_converter, "api_key", "test"), patch.object(httpx.AsyncClient, "post", outbound):
            answer = self.client.post("/chat", json={"messages":[{"role":"user","content":"hello"}]})
        self.assertEqual(answer.json(), {"text":"Welcome"})
        self.assertEqual(outbound.call_args.args[0], main.sop_converter.api_url)

    def test_call_workflow(self):
        self.login()
        encoded = io.BytesIO()
        with wave.open(encoded, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "uncompressed"))
            wav.writeframes(bytes(32000))
        files = {"file": ("sample.wav", encoded.getvalue(), "audio/wav")}
        stt, nlp, audio = Mock(), Mock(), Mock()
        seen = []
        def transcribe(path):
            self.assertTrue(Path(path).exists())
            seen.append(Path(path))
            return ([{"start":0,"end":1,"speaker":"Unknown","text":"Hello"}],
                    SimpleNamespace(language="en", language_probability=1, duration=1))
        stt.process_call.side_effect = transcribe
        nlp.clean_text.side_effect = lambda text: text
        nlp.segment_transcript.return_value = {"Greeting":[{}]}
        nlp.get_sentiment_trajectory.return_value = []
        audio.trim_silences.return_value = (False, 1, 1)
        rules = {"sop_rules":{"Greeting":{"steps":[{"text":"Greet"}],"weight":10}}}
        data = {"region":"Delhi","sop_rules":json.dumps(rules)}
        with patch.object(main,"speech_services",return_value=(stt,nlp,audio)), patch.object(
            main.sop_engine.llm_service,"evaluate_call",return_value={"Greeting::0":{"status":"PASS"}}) as judge:
            for route in ("/analyze-call/", "/analyze-long-call/"):
                result = self.client.post(route, files=files, data=data)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["evaluation"]["scoring"]["final_score"], 100)
            judge.return_value = {}
            self.assertEqual(self.client.post("/analyze-call/", files=files, data=data).status_code, 503)
        self.assertTrue(all(not path.exists() for path in seen))
        self.assertEqual(self.client.get("/insights?region=Delhi").json()["total_calls"], 2)
        self.assertEqual(self.client.get("/insights?region=Pune").json()["total_calls"], 0)
        self.login("two")
        self.assertEqual(self.client.get("/insights").json()["total_calls"], 0)

    def test_legacy_migration(self):
        self.legacy.write_text(json.dumps([{"call_id":"old","evaluation":{
            "scoring":{"final_score":80},"sop_adherence":{"Greeting":{"steps":[{"status":"FAIL","step":"Greet"}]}}}}]))
        for _ in range(2):
            db = DBService(self.legacy)
            self.assertEqual(db.get_aggregated_insights()["total_calls"], 1)
            self.assertEqual(db.get_aggregated_insights()["common_sop_failures"][0]["step"], "Greet")
            self.assertEqual(db.get_aggregated_insights(owner="one")["total_calls"], 0)

if __name__ == "__main__":
    unittest.main()
