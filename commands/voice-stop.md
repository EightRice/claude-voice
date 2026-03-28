# Stop Voice Service

Deregister this Claude instance and optionally shut down the voice service.

## Steps

1. **Deregister this instance**:
```bash
python -c "
import socket, json, os
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
msg = {'type': 'deregister', 'pid': os.getppid()}
s.sendto(json.dumps(msg).encode(), ('127.0.0.1', 9111))
s.close()
print('Deregistered from voice service')
"
```

2. **Check if service should shut down**: Ask if we should also stop the service entirely (it may be serving other Claude instances). If the user wants to stop it:
```bash
python -c "
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
msg = {'type': 'shutdown'}
s.sendto(json.dumps(msg).encode(), ('127.0.0.1', 9111))
s.close()
print('Shutdown signal sent to voice service')
"
```

3. **Report**: Confirm the instance was deregistered (and service stopped if requested).
