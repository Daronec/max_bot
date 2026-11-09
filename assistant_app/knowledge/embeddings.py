"""Local HTTP and offline E5 encoders for the knowledge index."""
import math
from pathlib import Path
import threading
from urllib.parse import urlsplit

import requests


class LocalEmbeddings:
    def __init__(self, base_url, model):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password:
            raise ValueError('Embeddings must use an explicitly configured local HTTP endpoint')
        self.url = base_url.rstrip('/') + '/embeddings'
        self.model = model

    def embed(self, texts):
        response = requests.post(self.url, json={'model': self.model, 'input': texts}, timeout=(3, 60),
                                 allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Local embedding request failed')
        data = sorted(response.json()['data'], key=lambda row: row['index'])
        vectors = [row['embedding'] for row in data]
        if (len(vectors) != len(texts) or not vectors or not vectors[0]
                or any(len(vector) != len(vectors[0]) or not all(
                    isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    for value in vector) for vector in vectors)):
            raise ValueError('Invalid local embeddings')
        return vectors


class E5Embeddings:
    """Offline CPU encoder; only reads an explicitly provisioned local checkpoint."""

    def __init__(self, directory, model):
        self.directory = Path(directory).resolve()
        self.model = model
        self._lock = threading.Lock()
        self._encoder = None

    def embed(self, texts, *, query=False):
        import torch
        from transformers import AutoModel, AutoTokenizer

        with self._lock:
            if self._encoder is None:
                tokenizer = AutoTokenizer.from_pretrained(self.directory, local_files_only=True)
                model = AutoModel.from_pretrained(self.directory, local_files_only=True, use_safetensors=True).eval()
                self._encoder = tokenizer, model
            tokenizer, model = self._encoder
            prefix = 'query: ' if query else 'passage: '
            batch = tokenizer([prefix + text for text in texts], max_length=512,
                              padding=True, truncation=True, return_tensors='pt')
            with torch.inference_mode():
                output = model(**batch).last_hidden_state
                mask = batch['attention_mask'].unsqueeze(-1).bool()
                pooled = output.masked_fill(~mask, 0).sum(1) / mask.sum(1).clamp(min=1)
                return torch.nn.functional.normalize(pooled, dim=1).tolist()


EMBEDDING_ERRORS = (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError)
