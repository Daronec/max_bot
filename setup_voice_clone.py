"""Install the optional isolated MLX voice-cloning runtime and pinned model."""
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
MODEL_ID = 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16'
MODEL_REVISION = '1eccf1cb2519b5a4e8a95b5f0544f3303568164f'


def main():
    if sys.version_info < (3,10):
        interpreter = ROOT/'.venv/bin/python'
        if not interpreter.is_file():raise SystemExit('Нужен Python 3.10+; сначала подготовьте .venv проекта.')
        os.execv(str(interpreter),[str(interpreter),str(Path(__file__).resolve())])
    environment = ROOT/'.venv-tts'
    if not (environment/'bin/python').is_file():venv.EnvBuilder(with_pip=True).create(environment)
    python = environment/'bin/python'
    subprocess.run([str(python),'-m','pip','install','--no-cache-dir','-r',str(ROOT/'requirements-tts.txt')],check=True)
    variables = dict(os.environ,HF_HOME=str(ROOT/'.models/hf-tts'),HF_HUB_DISABLE_TELEMETRY='1')
    script = '''from huggingface_hub import snapshot_download
import sys
snapshot_download(sys.argv[1],revision=sys.argv[2],local_dir=sys.argv[3],token=False,max_workers=4)
'''
    subprocess.run([str(python),'-c',script,MODEL_ID,MODEL_REVISION,str(ROOT/'.models/tts/qwen3-tts-0.6b-base')],
                   cwd=ROOT,env=variables,check=True)
    for name in ('inbox','profiles','generated'):
        folder = ROOT/'.voices'/name
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        folder.chmod(0o700)
    (ROOT/'.voices').chmod(0o700)
    subprocess.run([str(python),str(ROOT/'voice_clone.py'),'status','--load'],cwd=ROOT,check=True)


if __name__=='__main__':main()
