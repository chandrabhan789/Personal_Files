
# 🧠 MeetingMind AI — Meeting Assistant with AI Vision

A Streamlit app that shows your meeting tab on the left and an AI assistant on the right.
Ask questions, take screenshots, and transcribe audio — all in one browser window.

---

## 🚀 Quick Start (Local)

```bash
# 1. Clone / copy this folder
cd meeting-ai-assistant

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
streamlit run app.py
```

Then open http://localhost:8501 in **Chrome** (required for screen capture + audio).

---

## ☁️ Deploy on Streamlit Cloud (Free)

1. Push this folder to a **GitHub repo**
2. Go to https://share.streamlit.io → New app → Select your repo
3. Set **Main file path** to `app.py`
4. Add your API key as a **Secret**:
   - Key: `ANTHROPIC_API_KEY`
   - Value: `sk-ant-...`
5. Click Deploy ✅

---

## 🔑 API Key

**Currently using: OpenAI GPT-4o**

Get your OpenAI API key at https://platform.openai.com/api-keys

You can either:
- Enter it directly in the app's ⚙️ API Configuration section
- Set as environment variable: `OPENAI_API_KEY=sk-...`
- Add as a Streamlit Cloud secret: Key = `OPENAI_API_KEY`

---

## 🔮 Switching to Claude (Future)

When you get your Anthropic API key, only 3 small changes needed in `app.py`:

```python
# 1. Replace the import at the top
from openai import OpenAI  →  import anthropic

# 2. Change the 3 config constants
AI_PROVIDER  = "anthropic"
AI_MODEL     = "claude-opus-4-5"
ENV_KEY_NAME = "ANTHROPIC_API_KEY"

# 3. In get_ai_response(): comment out the OpenAI block,
#    uncomment the Anthropic block (it's already written for you!)
```

And in `requirements.txt`:
```
# Remove:  openai>=1.30.0
# Add:     anthropic>=0.25.0
```

That's it — all other code stays the same. ✅

---

## 🛠️ Features

| Feature | How it works |
|---|---|
| 📺 Tab capture | Browser `getDisplayMedia` API |
| 📸 Screenshot → AI | Canvas capture → Claude Vision API |
| 🎙 Speech transcription | Web Speech API (Chrome only) |
| 💬 AI Chat | Claude claude-opus-4-5 with multi-turn history |
| 📁 Manual screenshot upload | Fallback for non-Chrome browsers |

---

## ⚠️ Browser Requirements

| Feature | Chrome | Firefox | Safari |
|---|---|---|---|
| Tab capture | ✅ | ⚠️ Partial | ❌ |
| Audio capture | ✅ | ❌ | ❌ |
| Speech API | ✅ | ❌ | ❌ |
| Screenshot | ✅ | ⚠️ | ❌ |

**Best experience: Chrome on Windows or Mac.**

---

## 📁 File Structure

```
meeting-ai-assistant/
├── app.py              ← Main Streamlit app
├── requirements.txt    ← Python dependencies
└── README.md           ← This file
```

---

## 💡 Usage Tips

1. Open your meeting (Google Meet / Teams / Zoom web) in another Chrome tab
2. Launch this app in a separate Chrome window
3. Click **"📺 Share Tab"** and select your meeting tab
4. Use **"📸 Screenshot → AI"** to snap the current screen and send to AI
5. Type your question in the chat — the AI will answer with full context
6. Use **"🎙 Start Listening"** to transcribe speech, then **"↗ Use as AI Question"**
