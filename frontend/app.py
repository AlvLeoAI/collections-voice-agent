import streamlit as st
import httpx
import sys
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from src.voice_handler import VoiceHandler

load_dotenv()

API_URL = "http://localhost:8000"

st.set_page_config(page_title="Outbound Agent Sandbox", page_icon="🤖", layout="wide")

# Initialize Voice Handler locally for audio playback
if "voice_handler" not in st.session_state:
    st.session_state.voice_handler = VoiceHandler()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "call_state" not in st.session_state:
    st.session_state.call_state = None  # Dict from API
if "call_id" not in st.session_state:
    st.session_state.call_id = None
if "last_audio_digest" not in st.session_state:
    st.session_state.last_audio_digest = None
if "voice_input_nonce" not in st.session_state:
    st.session_state.voice_input_nonce = 0
if "pending_voice_digest" not in st.session_state:
    st.session_state.pending_voice_digest = None
if "voice_preview_text" not in st.session_state:
    st.session_state.voice_preview_text = ""
if "voice_preview_latency_ms" not in st.session_state:
    st.session_state.voice_preview_latency_ms = None
if "voice_preview_confidence" not in st.session_state:
    st.session_state.voice_preview_confidence = None

# --- Context setup (Mock data) ---
TEST_ACCOUNT_PRESETS = {
    "Austin Demo (78701)": {
        "target_name": "Alex Morgan",
        "callback_number": "+1 (555) 010-2000",
        "case_id": "CASE_DEMO_001",
        "amount_due": "240.00",
        "expected_zip": "78701",
    },
    "Dallas Demo (75201)": {
        "target_name": "Jamie Lee",
        "callback_number": "+1 (555) 010-3000",
        "case_id": "CASE_DEMO_002",
        "amount_due": "315.00",
        "expected_zip": "75201",
    },
    "Miami Demo (33101)": {
        "target_name": "Sam Rivera",
        "callback_number": "+1 (555) 010-4000",
        "case_id": "CASE_DEMO_003",
        "amount_due": "180.00",
        "expected_zip": "33101",
    },
}

DEFAULT_PRESET_LABEL = list(TEST_ACCOUNT_PRESETS.keys())[0]
if "selected_test_profile" not in st.session_state:
    st.session_state.selected_test_profile = DEFAULT_PRESET_LABEL
if "loaded_test_profile" not in st.session_state:
    st.session_state.loaded_test_profile = None
if "profile_target_name" not in st.session_state:
    st.session_state.profile_target_name = TEST_ACCOUNT_PRESETS[DEFAULT_PRESET_LABEL]["target_name"]
if "profile_callback_number" not in st.session_state:
    st.session_state.profile_callback_number = TEST_ACCOUNT_PRESETS[DEFAULT_PRESET_LABEL]["callback_number"]
if "profile_case_id" not in st.session_state:
    st.session_state.profile_case_id = TEST_ACCOUNT_PRESETS[DEFAULT_PRESET_LABEL]["case_id"]
if "profile_amount_due" not in st.session_state:
    st.session_state.profile_amount_due = TEST_ACCOUNT_PRESETS[DEFAULT_PRESET_LABEL]["amount_due"]
if "profile_expected_zip" not in st.session_state:
    st.session_state.profile_expected_zip = TEST_ACCOUNT_PRESETS[DEFAULT_PRESET_LABEL]["expected_zip"]

policy_config = {
    "brand_name": "Northstar Recovery",
    "disclosures": {"post_verification_disclosure_text": "This is Northstar Recovery. This is an attempt to collect a debt."},
    "limits": {"max_total_turns": 25}
}

# --- Sidebar ---
with st.sidebar:
    profile_inputs_disabled = st.session_state.call_state is not None

    st.title("🧪 Test Account")
    selected_profile = st.selectbox(
        "Preset",
        options=list(TEST_ACCOUNT_PRESETS.keys()) + ["Custom"],
        key="selected_test_profile",
        disabled=profile_inputs_disabled,
    )

    if (not profile_inputs_disabled) and selected_profile != st.session_state.loaded_test_profile:
        if selected_profile in TEST_ACCOUNT_PRESETS:
            preset = TEST_ACCOUNT_PRESETS[selected_profile]
            st.session_state.profile_target_name = preset["target_name"]
            st.session_state.profile_callback_number = preset["callback_number"]
            st.session_state.profile_case_id = preset["case_id"]
            st.session_state.profile_amount_due = preset["amount_due"]
            st.session_state.profile_expected_zip = preset["expected_zip"]
        st.session_state.loaded_test_profile = selected_profile

    st.text_input("Target name", key="profile_target_name", disabled=profile_inputs_disabled)
    st.text_input("Callback number", key="profile_callback_number", disabled=profile_inputs_disabled)
    st.text_input("Case ID", key="profile_case_id", disabled=profile_inputs_disabled)
    st.text_input("Amount due", key="profile_amount_due", disabled=profile_inputs_disabled)
    st.text_input("Expected ZIP", key="profile_expected_zip", disabled=profile_inputs_disabled)

    if profile_inputs_disabled:
        st.caption("Profile is locked during an active call. Click Reset Call to switch ZIP/profile.")

    st.title("🛡️ API Call State")
    if st.session_state.call_id:
        st.caption(f"Call ID: `{st.session_state.call_id}`")
    if st.session_state.call_state:
        cs = st.session_state.call_state
        st.json(cs)
    
    if st.button("Reset Call"):
        st.session_state.messages = []
        st.session_state.call_state = None
        st.session_state.call_id = None
        st.session_state.last_audio_digest = None
        st.session_state.voice_input_nonce = st.session_state.voice_input_nonce + 1
        st.session_state.pending_voice_digest = None
        st.session_state.voice_preview_text = ""
        st.session_state.voice_preview_latency_ms = None
        st.session_state.voice_preview_confidence = None
        st.rerun()

party_profile = {
    "target_name": st.session_state.profile_target_name,
    "callback_number": st.session_state.profile_callback_number,
}
account_context = {
    "case_id": st.session_state.profile_case_id,
    "amount_due": st.session_state.profile_amount_due,
    "expected_zip": st.session_state.profile_expected_zip,
}

st.title("🤖 Outbound Agent (FastAPI + Streamlit)")


def _format_rate(value):
    if isinstance(value, (int, float)):
        return f"{value * 100:.1f}%"
    return "N/A"


def _format_minutes(value):
    if isinstance(value, (int, float)):
        return f"{value:.1f} min"
    return "N/A"


with st.container():
    st.subheader("Demo Metrics")
    try:
        metrics_response = httpx.get(f"{API_URL}/metrics/summary", timeout=5.0)
        if metrics_response.status_code == 200:
            metrics = metrics_response.json()
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Calls", metrics.get("calls_total", 0))
            col2.metric("Ended Calls", metrics.get("ended_calls", 0))
            col3.metric("PTP Success (Ended)", _format_rate(metrics.get("ptp_success_rate_ended")))
            col4.metric("Avg Time to PTP", _format_minutes(metrics.get("avg_time_to_ptp_minutes")))

            trend_rows = metrics.get("daily", [])
            trend_table = []
            for row in trend_rows:
                if row.get("date") == "unknown":
                    continue
                trend_table.append(
                    {
                        "Date": row.get("date"),
                        "Calls": row.get("calls_total", 0),
                        "Ended": row.get("ended_calls", 0),
                        "PTP (Ended)": row.get("ptp_calls_ended", 0),
                        "PTP Success": _format_rate(row.get("ptp_success_rate_ended")),
                    }
                )
            if trend_table:
                st.caption("Recent Daily Trend")
                st.dataframe(trend_table, width="stretch", hide_index=True)

            job_metrics = metrics.get("jobs", {})
            if isinstance(job_metrics, dict) and job_metrics:
                j1, j2, j3 = st.columns(3)
                j1.metric("Queued Jobs Seen", job_metrics.get("jobs_total", 0))
                j2.metric("Blocked Policy", job_metrics.get("blocked_policy_total", 0))
                j3.metric("Blocked Suppression", job_metrics.get("blocked_suppression_total", 0))
        else:
            st.warning("Metrics endpoint is unavailable.")
    except Exception:
        st.warning("Could not load metrics from backend.")

is_call_ended = bool(st.session_state.call_state and st.session_state.call_state.get("phase") == "ended")
if is_call_ended:
    st.info("Call ended. Click 'Reset Call' in the sidebar to start a new one.")


def _clear_pending_voice() -> None:
    st.session_state.pending_voice_digest = None
    st.session_state.voice_preview_latency_ms = None
    st.session_state.voice_preview_confidence = None


def _reset_voice_input_widget() -> None:
    # Forces Streamlit to mount a fresh input widget so the next audio can be captured.
    st.session_state.voice_input_nonce = st.session_state.voice_input_nonce + 1
    st.session_state.last_audio_digest = None


def _estimate_transcript_confidence(transcript: str) -> float:
    text = (transcript or "").strip()
    if not text:
        return 0.05

    words = [w for w in text.split() if w.strip()]
    alpha_chars = sum(1 for ch in text if ch.isalpha())
    visible_chars = sum(1 for ch in text if not ch.isspace())
    alpha_ratio = (alpha_chars / visible_chars) if visible_chars else 0.0

    score = 0.35
    score += min(len(words) / 12.0, 1.0) * 0.35
    score += max(0.0, min(alpha_ratio, 1.0)) * 0.30

    noise_markers = ["???", "...", "[", "]", "(noise)", "(inaudible)"]
    lowered = text.lower()
    if any(marker in lowered for marker in noise_markers):
        score -= 0.15

    return max(0.05, min(score, 0.98))


def _run_user_turn(user_input: str) -> None:
    st.session_state.messages.append({"role": "user", "content": user_input})

    tz = "America/Chicago"
    now = datetime.now(ZoneInfo(tz))
    turn_event = {
        "event_type": "user_utterance",
        "transcript": user_input,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "current_local_date": now.date().isoformat(),
        "current_local_time": now.strftime("%H:%M"),
        "timezone": tz,
        "language": "en-US"
    }

    with st.spinner("Agent processing..."):
        payload = {
            "call_id": st.session_state.call_id,
            "turn_event": turn_event,
            "call_state": st.session_state.call_state,
            "party_profile": party_profile,
            "account_context": account_context,
            "policy_config": policy_config
        }
        response = httpx.post(f"{API_URL}/call/turn", json=payload)

        if response.status_code == 200:
            data = response.json()
            st.session_state.call_state = data["call_state"]
            st.session_state.messages.append({"role": "assistant", "content": data["assistant_text"]})

            with st.chat_message("assistant"):
                st.write(data["assistant_text"])
                audio_path = st.session_state.voice_handler.text_to_speech(data["assistant_text"])
                st.audio(audio_path)
            st.rerun()
        else:
            st.error(f"API Error: {response.text}")

# --- Call Logic ---
if st.session_state.call_state is None:
    # Start the call via API
    with st.spinner("Connecting to Agent API..."):
        response = httpx.post(f"{API_URL}/call/start", json={"party_profile": party_profile})
        if response.status_code == 200:
            data = response.json()
            st.session_state.call_id = data["call_id"]
            st.session_state.call_state = data["call_state"]
            st.session_state.messages.append({"role": "assistant", "content": data["assistant_text"]})
            # Play initial audio
            audio_path = st.session_state.voice_handler.text_to_speech(data["assistant_text"])
            st.audio(audio_path)
        else:
            st.error("Failed to connect to API. Is the server running?")

# Display messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

# Voice Input (MVP): turn-based.
voice_widget_key = f"voice_input_{st.session_state.voice_input_nonce}"
supports_audio_input = hasattr(st, "audio_input")
if supports_audio_input:
    st.caption("Mic mode active: record directly in your browser.")
    voice_audio = st.audio_input(
        "Record your response",
        disabled=is_call_ended,
        key=voice_widget_key,
    )
else:
    st.warning(
        "Direct mic capture is unavailable in this Streamlit version. "
        "Please upgrade Streamlit, or upload a short audio clip."
    )
    voice_audio = None if is_call_ended else st.file_uploader(
        "Upload recorded response",
        type=["wav", "mp3", "m4a", "ogg"],
        accept_multiple_files=False,
        key=voice_widget_key,
    )

if voice_audio and not is_call_ended:
    audio_bytes = voice_audio.getvalue()
    audio_digest = hashlib.sha256(audio_bytes).hexdigest()
    if audio_digest != st.session_state.last_audio_digest:
        st.session_state.last_audio_digest = audio_digest
        tmp_dir = Path("runtime/tmp_audio")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        audio_path = tmp_dir / f"user_input_{audio_digest[:16]}.wav"
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        with st.spinner("Transcribing audio..."):
            start_ts = time.perf_counter()
            transcript = st.session_state.voice_handler.transcribe_audio(str(audio_path)).strip()
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

        if transcript:
            st.session_state.pending_voice_digest = audio_digest
            st.session_state.voice_preview_text = transcript
            st.session_state.voice_preview_latency_ms = elapsed_ms
            st.session_state.voice_preview_confidence = _estimate_transcript_confidence(transcript)
        else:
            st.warning("I could not transcribe that audio. Please try again.")

if st.session_state.pending_voice_digest and not is_call_ended:
    conf = st.session_state.voice_preview_confidence
    latency = st.session_state.voice_preview_latency_ms
    if isinstance(conf, (float, int)) and conf >= 0.75:
        conf_label = "High"
        conf_color = "#2e7d32"
    elif isinstance(conf, (float, int)) and conf >= 0.50:
        conf_label = "Medium"
        conf_color = "#f9a825"
    else:
        conf_label = "Low"
        conf_color = "#c62828"

    st.caption("Voice transcript preview")
    if conf is not None or latency is not None:
        conf_text = f"{conf_label} ({float(conf) * 100:.0f}% est.)" if conf is not None else "N/A"
        latency_text = f"{float(latency):.0f} ms" if latency is not None else "N/A"
        st.markdown(
            (
                "Transcription quality hint: confidence "
                f"<span style='color: {conf_color}; font-weight: 600;'>{conf_text}</span>"
                f" | latency <span style='font-weight: 600;'>{latency_text}</span>"
            ),
            unsafe_allow_html=True,
        )
    st.text_area(
        "Edit before sending",
        key="voice_preview_text",
        height=120,
    )
    preview_send_col, preview_discard_col = st.columns(2)
    if preview_send_col.button("Send voice transcript", type="primary"):
        preview_text = st.session_state.voice_preview_text.strip()
        if preview_text:
            _clear_pending_voice()
            _reset_voice_input_widget()
            _run_user_turn(preview_text)
        else:
            st.warning("Transcript is empty. Please record again.")
    if preview_discard_col.button("Discard recording"):
        _clear_pending_voice()
        _reset_voice_input_widget()
        st.rerun()

# User Input
if user_input := st.chat_input("Type your response...", disabled=is_call_ended):
    _clear_pending_voice()
    _run_user_turn(user_input)
