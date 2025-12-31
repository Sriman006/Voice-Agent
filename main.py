import asyncio
import base64
import os
import json
import websockets

from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect

from dotenv import load_dotenv

# RAG imports
from pinecone import Pinecone
from openai import OpenAI

# ============================
# Load environment variables
# ============================
load_dotenv()

# FastAPI App
app = FastAPI()

# Twilio
twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

# OpenAI
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Pinecone
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
index = pc.Index("skillorea-voice")   # your index name

# ====================================
# Deepgram WebSocket Connection
# ====================================
def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("Deepgram API key missing")

    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )

# ====================================
# Load config.json for Deepgram
# ====================================
def load_config():
    with open("config.json", "r") as s:
        return json.load(s)

# ====================================
# Pinecone RAG: Retrieve context
# ====================================
async def rag_retrieve(query: str) -> str:
    emb = await asyncio.to_thread(
        openai_client.embeddings.create,
        model="text-embedding-3-small",
        input=query,
    )
    vector = emb.data[0].embedding

    res = index.query(
        vector=vector,
        top_k=3,
        include_metadata=True
    )

    contexts = []
    for match in res.matches:
        if match.metadata and "answer" in match.metadata:
            contexts.append(match.metadata["answer"])

    return "\n".join(contexts) if contexts else "No relevant context."

# ====================================
# Deepgram Transcript Extractor
# ====================================
def _extract_transcript_from_deepgram(decoded: dict):
    transcript = ""
    is_final = False
    response_id = decoded.get("response_id") or decoded.get("id")

    # Direct fields
    transcript = decoded.get("transcript") or decoded.get("text") or ""

    if "is_final" in decoded:
        is_final = decoded.get("is_final")

    # Alternatives block
    if not transcript and "alternatives" in decoded:
        alts = decoded.get("alternatives", [])
        if alts:
            transcript = alts[0].get("transcript") or alts[0].get("text") or ""
            if alts[0].get("final") or alts[0].get("is_final"):
                is_final = True

    return transcript.strip(), bool(is_final), response_id

# ====================================
# Handle barge-in
# ====================================
async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded.get("type") == "UserStartedSpeaking":
        await twilio_ws.send_text(json.dumps({
            "event": "clear",
            "streamSid": streamsid
        }))

# ====================================
# MAIN AI HANDLER — RAG + Deepgram Thinking
# ====================================
async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)

    transcript, is_final, response_id = _extract_transcript_from_deepgram(decoded)

    if not transcript:
        return

    if not is_final:
        print("Partial:", transcript)
        return

    print("Final transcript:", transcript)

    # ----------- RAG Retrieve ----------------
    try:
        context = await rag_retrieve(transcript)
    except Exception as e:
        print("RAG error:", e)
        context = "No relevant context."

    # ----------- Deepgram Thinking Prompt -----
    
    instructions = f"""
You are Skillorea AI, the official voice assistant of the Skillorea educational platform.

Your purpose:
- Introduce and promote the Skillorea.
- Explain features, benefits, courses, and offerings of Skillorea.
- Answer user questions ONLY using the information provided in the retrieved context.
- Do NOT create information that is not present in the context.
- Do NOT use external knowledge outside the context.
- If the user asks something not present in the context, politely say:
  “I don’t have that information in my Skillorea knowledge base.”

Speaking Style:
- Friendly, simple, and clear.
- Sound like an app promoter, not a teacher.
- Keep answers short and direct unless the user asks for details.
- If the user seems confused, re-explain the feature simply.

Rules:
1. ALWAYS base your answer only on the RAG context.
2. NEVER invent or guess.
3. NEVER answer using general knowledge.
4. If context is missing or unrelated, say you don’t have that information.
5. Your identity is ONLY “Skillorea AI,” not a tutor or medical/technical expert.

User Question:
{transcript}

Retrieved Context (your ONLY knowledge):
{context}

Using ONLY the retrieved context, respond as the Skillorea app’s voice assistant.
If the context does not contain the answer, clearly state that the information is not available.

"""

    payload = {
        "type": "response.create",
        "response_id": response_id,
        "instructions": instructions.strip()
    }

    print("Sending response.create to Deepgram...")
    await sts_ws.send(json.dumps(payload))

# ====================================
# Send audio to Deepgram
# ====================================
async def sts_sender(sts_ws, audio_queue):
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)

# ====================================
# Receive Deepgram messages
# ====================================
async def sts_receiver(sts_ws, twilio_ws, streamsid_queue):
    streamsid = await streamsid_queue.get()

    async for message in sts_ws:
        if isinstance(message, str):
            decoded = json.loads(message)
            print("Deepgram:", decoded)
            await handle_text_message(decoded, twilio_ws, sts_ws, streamsid)
            continue

        # Binary Mu-law audio
        raw_mulaw = message
        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {
                "payload": base64.b64encode(raw_mulaw).decode("utf-8")
            }
        }
        await twilio_ws.send_text(json.dumps(media_message))

# ====================================
# Receive audio from Twilio
# ====================================
async def twilio_receiver(twilio_ws, audio_queue, streamsid_queue):
    BUFFER_SIZE = 160 * 20
    inbuffer = bytearray()

    async for message in twilio_ws.iter_text():
        try:
            data = json.loads(message)
            event = data["event"]

            if event == "start":
                streamsid = data["start"]["streamSid"]
                streamsid_queue.put_nowait(streamsid)

            elif event == "media":
                chunk = base64.b64decode(data["media"]["payload"])
                inbuffer.extend(chunk)

            elif event == "stop":
                print("Twilio STOP")
                break

            # Feed Deepgram fixed size chunks
            while len(inbuffer) >= BUFFER_SIZE:
                audio_queue.put_nowait(inbuffer[:BUFFER_SIZE])
                inbuffer = inbuffer[BUFFER_SIZE:]

        except Exception as e:
            print("Twilio error:", e)
            break

# ====================================
# OUTBOUND CALL ENDPOINT
# ====================================
class CallRequest(BaseModel):
    to_number: str

@app.post("/make-call")
async def make_call(request: CallRequest):
    public_url = os.getenv("PUBLIC_URL")
    if not public_url:
        return {"error": "Set PUBLIC_URL"}

    if not public_url.startswith("http"):
        public_url = "https://" + public_url

    call = twilio_client.calls.create(
        to=request.to_number,
        from_=os.getenv("TWILIO_PHONE_NUMBER"),
        url=f"{public_url}/voice"
    )

    return {"message": "Call started", "sid": call.sid}

# ====================================
# Twilio Voice Entry (TwiML)
# ====================================
@app.post("/voice")
async def voice_start(request: Request):
    
    form = await request.form()
    direction = form.get("Direction")  # inbound | outbound-api

    # Prepare TwiML response
    resp = VoiceResponse()

    # Different greeting based on call direction
    if direction == "inbound":
        resp.say(
            "Welcome to Skillorea. You are now connected to our AI assistant."
        )
    else:
        resp.say(
            "Connecting you to Skillorea Voice agent."
        )

    # Prepare WebSocket stream URL
    public_url = os.getenv("PUBLIC_URL")
    if not public_url:
        # Failsafe (Twilio requires public HTTPS)
        return Response(
            content="PUBLIC_URL not configured",
            status_code=500
        )

    ws_url = (
        public_url
        .replace("https://", "wss://")
        .replace("http://", "ws://")
    )

    # Connect Twilio Media Stream
    connect = Connect()
    connect.stream(url=f"{ws_url}/twilio")

    resp.append(connect)

    # IMPORTANT: always return to_xml()
    return Response(
        content=resp.to_xml(),
        media_type="application/xml"
    )


# ====================================
# WebSocket Endpoint for Twilio ↔ Deepgram
# ====================================
@app.websocket("/twilio")
async def twilio_handler(twilio_ws: WebSocket):
    await twilio_ws.accept()
    print("Twilio WebSocket connected.")

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        config = load_config()
        await sts_ws.send(json.dumps(config))

        await asyncio.gather(
            sts_sender(sts_ws, audio_queue),
            sts_receiver(sts_ws, twilio_ws, streamsid_queue),
            twilio_receiver(twilio_ws, audio_queue, streamsid_queue)
        )

# ====================================
# Start server
# ====================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
