# Setup Kokoro TTS

Download the Kokoro ONNX model files for high-quality local text-to-speech.

This downloads ~350MB of model files. No HuggingFace token is required (Apache licensed).

## Steps

1. **Create models directory**:
```bash
python -c "
import os
data_dir = os.environ.get('CLAUDE_PLUGIN_DATA', os.path.expanduser('~/.claude/plugins/data/claude-voice'))
models_dir = os.path.join(data_dir, 'models')
os.makedirs(models_dir, exist_ok=True)
print(models_dir)
"
```

2. **Download kokoro-v1.0.onnx** (~325MB):
```bash
curl -L --progress-bar -o "${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/claude-voice}/models/kokoro-v1.0.onnx" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
```

3. **Download voices-v1.0.bin** (~28MB):
```bash
curl -L --progress-bar -o "${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/claude-voice}/models/voices-v1.0.bin" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
```

4. **Install kokoro-onnx package**:
```bash
pip install kokoro-onnx
```

5. **Verify**: Check that both files exist and report their sizes:
```bash
python -c "
import os
data_dir = os.environ.get('CLAUDE_PLUGIN_DATA', os.path.expanduser('~/.claude/plugins/data/claude-voice'))
models_dir = os.path.join(data_dir, 'models')
for f in ['kokoro-v1.0.onnx', 'voices-v1.0.bin']:
    path = os.path.join(models_dir, f)
    if os.path.exists(path):
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f'  {f}: {size_mb:.1f} MB')
    else:
        print(f'  {f}: MISSING')
"
```

6. **Report**: Tell the user Kokoro is ready. They can switch to it by restarting the voice service with `--backend kokoro`, or by updating their plugin config to set `ttsBackend` to `kokoro`.
