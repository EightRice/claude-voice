# Voice Service Status

Check the status of the voice service.

## Steps

1. **Check if service is running**:
```bash
python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(('127.0.0.1', 9111))
    print('NOT_RUNNING')
except OSError:
    print('RUNNING')
finally:
    s.close()
"
```

2. **Check for voice_service.py process**:
```bash
tasklist /FI "IMAGENAME eq python*" /V 2>nul | findstr voice_service || echo "No voice_service process found"
```

3. **Report status** to the user:
   - Whether the service is running or not
   - If running, note that hooks (PreToolUse, Stop) are forwarding events for TTS narration
   - Mention available commands: `/voice-start`, `/voice-stop`, `/voice-setup-kokoro`
