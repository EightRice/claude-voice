# Start Voice Service

Start the claude-voice service and register this Claude instance.

## Steps

1. **Platform check**: Run `python -c "import sys; print(sys.platform)"` and verify it returns `win32`. If not, tell the user that claude-voice only supports Windows and stop.

2. **Check dependencies**: Run `python -c "import numpy, sounddevice, faster_whisper, edge_tts, miniaudio, keyboard"`. If any import fails, tell the user which package is missing and suggest `pip install -r <plugin_root>/requirements.txt`.

3. **Check if service is already running**: Run this Python snippet to UDP-ping port 9111:
```bash
python -c "
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(1)
s.sendto(json.dumps({'type': 'ping'}).encode(), ('127.0.0.1', 9111))
try:
    s.bind(('127.0.0.1', 9111))
    print('NOT_RUNNING')
except OSError:
    print('RUNNING')
finally:
    s.close()
"
```

4. **If NOT running**: Start the service as a detached background process:
```bash
python "${CLAUDE_PLUGIN_ROOT}/service/voice_service.py" --backend "${CLAUDE_VOICE_BACKEND:-edge}" &
```
Wait 2 seconds, then verify the port is occupied (repeat the check from step 3).

5. **Register this instance**: Send a register message via UDP:
```bash
python -c "
import socket, json, os
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
msg = {'type': 'register', 'name': os.environ.get('CLAUDE_NAME', 'claude'), 'pid': os.getpid()}
s.sendto(json.dumps(msg).encode(), ('127.0.0.1', 9111))
s.close()
print('Registered with voice service')
"
```

6. **Report status**: Tell the user the voice service is running and ready. Mention the push-to-talk key and that hooks are active for TTS narration.
