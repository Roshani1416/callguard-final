import json
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
import sqlite3
from collections import defaultdict
import config

class DBService:
    def __init__(self, db_path=None):
        directory = Path(os.getenv("CALLGUARD_DATA_DIR", Path(config.BASE_DIR).parent / "data"))
        legacy = Path(db_path) if db_path else directory / "calls.json"
        self.path = legacy.with_suffix(".sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, owner TEXT, record TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS migrations (source TEXT PRIMARY KEY)")
            conn.execute("BEGIN IMMEDIATE")
            if legacy.is_file() and not conn.execute("SELECT 1 FROM migrations WHERE source=?", (str(legacy),)).fetchone():
                for record in json.loads(legacy.read_text(encoding="utf-8-sig")):
                    conn.execute("INSERT OR IGNORE INTO calls VALUES (?,NULL,?)", (record["call_id"], json.dumps(record)))
                conn.execute("INSERT INTO migrations VALUES (?)", (str(legacy),))

    def save_call(self, call_data, owner=None):
        call_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO calls VALUES (?,?,?)", (call_data["call_id"], owner, json.dumps(call_data)))

    def get_calls(self, region=None, owner=None):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT record FROM calls WHERE (? IS NULL OR owner=?) ORDER BY rowid", (owner, owner))
            records = [json.loads(row[0]) for row in rows]
        return [c for c in records if not region or c.get("metadata", {}).get("region") == region]

    def get_aggregated_insights(self, region=None, owner=None):
        calls = self.get_calls(region, owner)
        if not calls:
            return {
                "total_calls": 0,
                "average_score": 0,
                "sop_pass_rate": 0,
                "common_sop_failures": [],
                "recent_calls_summary": [],
                "common_issues": [],
                "sentiment_trend": []
            }

        total_calls = len(calls)
        total_score = 0
        passed_sop_count = 0
        failed_sops = defaultdict(int)
        sentiment_scores = []
        
        for call in calls:
            evaluation = call.get("evaluation", {})
            scoring = evaluation.get("scoring", {})
            
            # Score
            score = scoring.get("final_score", 0) or 0
            if isinstance(score, str):
                try:
                    score = float(score.replace('%', ''))
                except ValueError:
                    score = 0
            total_score += score
            
            # SOP Adherence
            sop_results = evaluation.get("sop_adherence", [])
            if isinstance(sop_results, dict):
                sop_results = [step for section in sop_results.values() for step in section.get("steps", [])]
            all_passed = bool(sop_results) and all(step.get("status") == "PASS" for step in sop_results)
            for sop in sop_results:
                if sop.get("status") == "FAIL":
                    all_passed = False
                    step_name = sop.get("step", "Unknown Step")
                    failed_sops[step_name] += 1
            
            if all_passed:
                passed_sop_count += 1
                
            # Sentiment (taking average of trajectory if available, or just end sentiment)
            # This is a simplification; a real system might process the trajectory more deeply.
            # Assuming we can derive a simple sentiment metric here or just track count.
            
        avg_score = total_score / total_calls if total_calls > 0 else 0
        sop_pass_rate = (passed_sop_count / total_calls * 100) if total_calls > 0 else 0
        
        # Sort common issues
        sorted_issues = sorted(failed_sops.items(), key=lambda x: x[1], reverse=True)
        
        insights = {
            "region": region if region else "All Regions",
            "total_calls": total_calls,
            "average_score": round(avg_score, 2),
            "sop_pass_rate": round(sop_pass_rate, 2),
            "common_sop_failures": [
                {"step": k, "count": v, "percentage": round(v/total_calls*100, 1)} 
                for k, v in sorted_issues[:5]
            ],
            "recent_calls_summary": [
                 {
                     "call_id": c.get("call_id"),
                     "score": c.get("evaluation", {}).get("scoring", {}).get("final_score"),
                     "date": c.get("timestamp")
                 } for c in calls[-5:] # Last 5 calls
            ]
        }
        
        return insights
