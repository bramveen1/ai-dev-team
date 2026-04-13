import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
load_dotenv()

app = App(token=os.environ["LISA_BOT_TOKEN"])

@app.event("app_mention")
def handle_mention(event, say):
    say(f"Lisa is alive! You said: {event['text']}")

@app.event("message")
def handle_message(event, say):
    if event.get("channel_type") == "im":
        say(f"Lisa heard you in DM: {event['text']}")

handler = SocketModeHandler(app, os.environ["LISA_APP_TOKEN"])
handler.start()