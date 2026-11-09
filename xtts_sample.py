"""Generate a private comparison WAV with the local Coqui XTTS-v2 checkpoint."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import time
import uuid
import wave


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, help='WAV filename in .voices/inbox')
    parser.add_argument('--text-file', required=True, type=Path)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    inbox = ROOT / '.voices/inbox'
    reference = inbox / args.reference
    if (Path(args.reference).name != args.reference or reference.suffix.lower() != '.wav'
            or reference.is_symlink() or not reference.is_file() or reference.stat().st_size > 20 * 1024 * 1024):
        parser.error('Use a regular WAV file in .voices/inbox.')
    text = args.text_file.read_text(encoding='utf-8').strip()
    if not 1 <= len(text) <= 600:
        parser.error('Use a text of up to 600 characters.')
    checkpoint = ROOT / '.models/xtts-v2'
    if not all((checkpoint / name).is_file() for name in ('config.json', 'model.pth', 'vocab.json')):
        parser.error('The official XTTS-v2 checkpoint must already be downloaded.')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    import numpy as np
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    torch.set_num_threads(min(6, os.cpu_count() or 1))
    torch.manual_seed(args.seed)
    config = XttsConfig()
    config.load_json(str(checkpoint / 'config.json'))
    model = Xtts(config)
    started = time.monotonic()
    model.load_checkpoint(config, checkpoint_dir=str(checkpoint), eval=True, use_deepspeed=False)
    with torch.inference_mode():
        conditioning, speaker = model.get_conditioning_latents(audio_path=[str(reference)])
        output = model.inference(text, 'ru', conditioning, speaker, enable_text_splitting=True)
    samples = np.asarray(output['wav'], dtype=np.float32)
    if not samples.size or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 0.001:
        raise RuntimeError('XTTS returned unusable audio.')
    sample_rate = config.audio.output_sample_rate
    folder = ROOT / '.voices/generated'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = folder / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.wav')
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as file, wave.open(file, 'wb') as audio:
        audio.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm.tobytes())
    print(f'{destination}\nAudio: {len(pcm)/sample_rate:.2f} s; elapsed: {time.monotonic()-started:.1f} s')


if __name__ == '__main__':
    main()
