import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from google.auth.exceptions import InvalidValue
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_firebase_token
from pydantic import BaseModel, Field

import config
from db_service import DBService
from llm_service import SOPConverter, ProviderUnavailable
from policy_processor import PolicyProcessor
from scoring_service import ScoringService
from sop_engine import SOPEngine, EvaluationUnavailable

class BodyLimit:
    """Bound the request before multipart parsing can spool an unlimited upload."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        mib = 101 if path in {"/analyze-call", "/analyze-long-call"} else 11 if path == "/upload-policy" else 1
        limit = mib * 1024**2
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > limit:
            return await JSONResponse({"detail": "Request exceeds upload limit."}, 413)(scope, receive, send)
        consumed = 0
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                raise HTTPException(413, "Request exceeds upload limit.")
            return message
        await self.app(scope, bounded_receive, send)

app = FastAPI(title="CallGuard")
app.add_middleware(BodyLimit)
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in origins],
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
bearer = HTTPBearer(auto_error=False)
project = os.getenv("FIREBASE_PROJECT_ID", "callguard-35842")

def identity(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if credentials is None:
        raise HTTPException(401, "Sign in to continue.")
    try:
        claims = verify_firebase_token(credentials.credentials, Request(), audience=project)
        if claims.get("iss") != f"https://securetoken.google.com/{project}" or not claims.get("sub"):
            raise ValueError("Invalid token issuer or subject")
        return claims
    except (ValueError, InvalidValue):
        raise HTTPException(401, "Invalid or expired sign-in.")
    except Exception:
        raise HTTPException(503, "Sign-in verification unavailable.")

@lru_cache(maxsize=1)
def database():
    return DBService()

@lru_cache(maxsize=1)
def speech_services():
    from stt_service import STTService
    from nlp_processor import NLPProcessor
    from audio_processor import AudioProcessor
    return STTService(), NLPProcessor(), AudioProcessor()

sop_engine = SOPEngine()
scoring_service = ScoringService()
policy_processor = PolicyProcessor(config.POLICIES_DIR)
sop_converter = SOPConverter()
analysis_slot = asyncio.Semaphore(1)

@app.exception_handler(EvaluationUnavailable)
@app.exception_handler(ProviderUnavailable)
async def unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

@app.get("/")
def read_root():
    return {"message": "Welcome to Battery Smart Auto-QA System API"}

@app.get("/sop-rules")
@app.get("/sop_rules")
def get_sop_rules(user=Depends(identity)):
    return config.load_sop_rules()

def validate_rules(rules):
    sections = rules.get("sop_rules") if isinstance(rules, dict) else None
    if not isinstance(sections, dict) or not sections or len(sections) > 40:
        raise HTTPException(422, "Invalid SOP sections.")
    count = 0
    for details in sections.values():
        if not isinstance(details, dict) or not isinstance(details.get("steps"), list):
            raise HTTPException(422, "Invalid SOP checklist.")
        weight = details.get("weight", 0)
        if not isinstance(weight, (float, int)) or not 0 <= weight <= 10000:
            raise HTTPException(422, "Invalid SOP weight.")
        for step in details["steps"]:
            if not isinstance(step, dict) or not isinstance(step.get("text"), str) or not step["text"].strip():
                raise HTTPException(422, "Each SOP step needs text.")
            count += 1
    if not 1 <= count <= 100:
        raise HTTPException(422, "Use between 1 and 100 SOP steps.")
    return sections

def process_rules(rules, refresh=False):
    for details in validate_rules(rules).values():
        for step in details["steps"]:
            if refresh or not step.get("internal_intent"):
                step["internal_intent"] = sop_converter.convert_to_intent(step["text"])
    return rules

@app.post("/update-sop")
def update_sop(rules: dict, user=Depends(identity)):
    if user.get("admin") is not True:
        raise HTTPException(403, "Administrator access required.")
    rules.setdefault("sentiments", config.SOP_RULES["sentiments"])
    risks = rules["sentiments"].get("risk_keywords") if isinstance(rules["sentiments"], dict) else None
    if not isinstance(risks, list) or not all(isinstance(k, str) for k in risks):
        raise HTTPException(422, "Invalid risk keywords.")
    process_rules(rules, refresh=True)
    with tempfile.NamedTemporaryFile("w", dir=Path(config.SOP_RULES_PATH).parent, delete=False, encoding="utf-8") as f:
        yaml.safe_dump(rules, f)
    os.replace(f.name, config.SOP_RULES_PATH)
    config.SOP_RULES = rules
    sop_engine.rules = rules["sop_rules"]
    sop_engine.risks = risks
    return {"status": "success"}

@app.post("/process-sop-intents")
def process_sop_intents(rules: dict, user=Depends(identity)):
    return {"status": "success", "processed_rules": process_rules(rules)}

class Suggestion(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

@app.post("/generate-sop-suggestion")
def generate_sop_suggestion(request: Suggestion, user=Depends(identity)):
    return sop_converter.generate_sop_suggestion(request.text)

class Message(BaseModel):
    role: Literal["assistant", "user"]
    content: str = Field(min_length=1, max_length=4000)

class Chat(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=12)

@app.post("/chat")
async def chat(request: Chat, user=Depends(identity)):
    if not sop_converter.api_key:
        raise ProviderUnavailable("Groq is not configured.")
    messages = [{"role": "system", "content": "You are the Meleregamy concierge. Help with company services and call quality analysis."}]
    messages.extend(m.model_dump() for m in request.messages)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(sop_converter.api_url,
                headers={"Authorization": f"Bearer {sop_converter.api_key}"},
                json={"model": sop_converter.model, "messages": messages, "max_tokens": 500})
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Empty response")
            return {"text": answer}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderUnavailable("Groq chat is unavailable. Please retry.") from exc

def policy_key(user, sop_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", sop_id):
        raise HTTPException(422, "Invalid SOP identifier.")
    return hashlib.sha256(user["sub"].encode()).hexdigest() + "_" + sop_id

def save_upload(upload, dest, limit):
    size = 0
    with dest.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "File exceeds the upload limit.")
            output.write(chunk)
    if size == 0:
        raise HTTPException(422, "Empty upload.")

@app.post("/upload-policy")
def upload_policy(sop_id: str = Form(...), file: UploadFile = File(...), user=Depends(identity)):
    key = policy_key(user, sop_id)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "policy.pdf"
        save_upload(file, path, 10 * 1024**2)
        if not policy_processor.process_policy(path, key):
            raise HTTPException(422, "Upload a readable PDF with extractable text.")
    return {"status": "success", "sop_id": sop_id, "filename": file.filename}

@app.get("/insights")
def get_insights(region: Optional[str] = None, user=Depends(identity)):
    return database().get_aggregated_insights(region, None if user.get("admin") is True else user["sub"])

def analyze(file, sop_rules, sop_id, region, user, long_call):
    custom_rules = None
    if sop_rules:
        try:
            custom_rules = validate_rules(json.loads(sop_rules))
        except ValueError:
            raise HTTPException(422, "Invalid SOP JSON.")
    if sop_id:
        sop_id = policy_key(user, sop_id)
    file_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording"
        save_upload(file, path, 100 * 1024**2)
        # Check duration before decoding a compressed recording into memory.
        formats = ["-format_whitelist", "wav,mp3,flac,ogg,mov,matroska,aac,aiff,asf,amr", "-protocol_whitelist", "file"]
        try:
            probe = subprocess.run(["ffprobe", "-v", "error", *formats, "-select_streams", "a:0",
                "-show_entries", "stream=index:format=duration", "-of", "json", str(path)],
                capture_output=True, check=True, timeout=20)
            media = json.loads(probe.stdout)
            if not media.get("streams") or not 0 < float(media["format"]["duration"]) <= 7200:
                raise ValueError()
            decoded = Path(directory) / "decoded.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *formats, "-i", str(path), "-map", "0:a:0",
                            "-t", "7200", "-ac", "1", "-ar", "16000", str(decoded)],
                           capture_output=True, check=True, timeout=300)
            path = decoded
        except FileNotFoundError:
            raise HTTPException(503, "Audio tools are unavailable.")
        except (subprocess.SubprocessError, ValueError, KeyError):
            raise HTTPException(422, "Upload valid audio up to two hours long.")
        try:
            stt_service, nlp_processor, audio_processor = speech_services()
        except (OSError, ImportError, RuntimeError) as exc:
            raise HTTPException(503, "Speech models are unavailable. No score was recorded.") from exc
        if long_call:
            trimmed = Path(directory) / "trimmed.wav"
            success, orig_dur, new_dur = audio_processor.trim_silences(str(path), str(trimmed))
            if success:
                path = trimmed
        transcription, info = stt_service.process_call(str(path))
        # B. NLP Processing (Cleaning & Segmentation)
        for seg in transcription:
            seg["text"] = nlp_processor.clean_text(seg["text"])
        
        segmented_transcript = nlp_processor.segment_transcript(transcription)
        sentiment_trajectory = nlp_processor.get_sentiment_trajectory(transcription)
        
        # C. SOP & Scoring
        sop_results = sop_engine.check_adherence(transcription, segmented_transcript, rules=custom_rules, sop_id=sop_id)
        risks = sop_engine.detect_risks(transcription)
        resolution_status = sop_engine.validate_resolution(segmented_transcript, sop_results)
        
        scoring_summary = scoring_service.calculate_final_score(sop_results, sentiment_trajectory)
        coaching_insights = scoring_service.generate_coaching_insights(sop_results)
        alerts = scoring_service.generate_alerts(sop_results, risks, scoring_summary)
        
        
        result = {
            "call_id": file_id,
            "metadata": {
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "region": region
            },
            "transcript": transcription,
            "segmented_transcript_summary": {section: len(segs) for section, segs in segmented_transcript.items()},
            "evaluation": {
                "sop_adherence": sop_results,
                "resolution": resolution_status,
                "risks_detected": risks,
                "scoring": scoring_summary
            },
            "coaching_insights": coaching_insights,
            "supervisor_alerts": alerts
        }
        
        if long_call:
            result["metadata"].update(is_long_call=True, original_duration=orig_dur, trimmed_duration=orig_dur-new_dur)
        database().save_call(result, user["sub"])
        return result

async def run_analysis(file, sop_rules, sop_id, region, user, long_call):
    try:
        await asyncio.wait_for(analysis_slot.acquire(), timeout=10)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Another recording is being processed. Please retry.")
    try:
        return await run_in_threadpool(analyze, file, sop_rules, sop_id, region, user, long_call)
    finally:
        analysis_slot.release()

@app.post("/analyze-call/")
async def analyze_call(file: UploadFile = File(...), sop_rules: Optional[str] = Form(None),
        sop_id: Optional[str] = Form(None), region: Optional[str] = Form(None), user=Depends(identity)):
    return await run_analysis(file, sop_rules, sop_id, region, user, False)

@app.post("/analyze-long-call/")
async def analyze_long_call(file: UploadFile = File(...), sop_rules: Optional[str] = Form(None),
        sop_id: Optional[str] = Form(None), region: Optional[str] = Form(None), user=Depends(identity)):
    return await run_analysis(file, sop_rules, sop_id, region, user, True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
