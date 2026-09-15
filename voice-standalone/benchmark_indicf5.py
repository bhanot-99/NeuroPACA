import time
import os
import sys
import numpy as np
import soundfile as sf
import subprocess
import torch
import torchaudio
from transformers.dynamic_module_utils import get_class_from_dynamic_module

# Ensure torchaudio.load uses soundfile without needing torchcodec
def _safe_torchaudio_load(filepath, *args, **kwargs):
    data, sr = sf.read(filepath, dtype='float32')
    tensor = torch.from_numpy(data)
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    else:
        tensor = tensor.t()
    return tensor, sr

torchaudio.load = _safe_torchaudio_load

print("Step 1: Loading IndicF5 model...")
t0_load = time.perf_counter()
repo_id = "raajain/IndicF5"
INF5Model = get_class_from_dynamic_module(f"{repo_id}--model.INF5Model", repo_id)
INF5Config = get_class_from_dynamic_module(f"{repo_id}--model.INF5Config", repo_id)
config = INF5Config(name_or_path=repo_id)
model = INF5Model(config)
t1_load = time.perf_counter()
load_time = t1_load - t0_load
print(f"Model load time: {load_time:.2f}s")

ref_audio = "tts_reference_audio/PAN_F_HAPPY_00001.wav"
with open("tts_reference_audio/PAN_F_HAPPY_00001.txt", "r", encoding="utf-8") as f:
    ref_text = f.read().strip()

test_text = "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, ਇਹ ਇੱਕ ਟੈਸਟ ਹੈ"
print(f"\nStep 2: Synthesizing test Punjabi phrase: '{test_text}'")
print(f"Reference audio: {ref_audio}")
print(f"Reference text: {ref_text}")

t0_gen = time.perf_counter()
audio = model(
    test_text,
    ref_audio_path=ref_audio,
    ref_text=ref_text,
)
t1_gen = time.perf_counter()
gen_time = t1_gen - t0_gen
print(f"\nGeneration took: {gen_time:.2f}s")

audio = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio
output_path = "test_punjabi.wav"
sf.write(output_path, np.array(audio, dtype=np.float32), samplerate=24000)
print(f"Saved generated audio to {output_path} (size: {os.path.getsize(output_path)} bytes)")

print("\nStep 3: Playing generated audio via aplay...")
subprocess.run(["aplay", output_path], check=False)
print("Playback finished.")
