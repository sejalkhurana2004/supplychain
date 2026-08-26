"""
Entrypoint for Render -- exists purely so the Start Command field can be the
plain, no-special-characters "python main.py" instead of
"uvicorn app:app --host 0.0.0.0 --port $PORT", which Render's Start Command
field rejects (it doesn't allow ":" or "$").
"""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
