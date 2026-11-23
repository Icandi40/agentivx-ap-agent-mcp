# Narration for the carousel video: one WAV per slide from script.json.
# Kokoro TTS runs locally and is free (Apache-2.0 model); voice af_heart.
# Run with the Python that has kokoro-onnx installed:
#   C:\Python314\python.exe gen_vo.py
import json
import pathlib

import kokoro_onnx
import soundfile as sf

here = pathlib.Path(__file__).parent
build = here / "build" / "vo"
build.mkdir(parents=True, exist_ok=True)
cache = pathlib.Path.home() / ".cache" / "hyperframes" / "tts"
model = kokoro_onnx.Kokoro(
    str(cache / "models" / "kokoro-v1.0.onnx"), str(cache / "voices" / "voices-v1.0.bin")
)

lines = json.loads((here / "script.json").read_text(encoding="utf-8"))
out = []
for line in lines:
    samples, rate = model.create(line["say"], voice="af_heart", speed=1.0, lang="en-us")
    path = build / f"{line['id']}.wav"
    sf.write(path, samples, rate)
    out.append({**line, "wav": str(path), "seconds": round(len(samples) / rate, 3)})
    print(f"{line['id']}: {len(samples) / rate:.2f}s")
(here / "build" / "vo.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"total narration {sum(x['seconds'] for x in out):.1f}s")
