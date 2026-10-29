"""Download the MAX API CA chain into this project only.

Sources: https://www.gosuslugi.ru/crt
No system trust settings are modified and TLS verification is never disabled.
"""
from pathlib import Path
import ssl
import requests

BASE_DIR = Path(__file__).resolve().parent
CERTIFICATE_URLS = (
    'https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt',
    'https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt',
)


def main():
    certificates = [Path(requests.certs.where()).read_text(encoding='ascii').strip()]
    for url in CERTIFICATE_URLS:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        certificate = response.text.strip()
        if not certificate.startswith('-----BEGIN CERTIFICATE-----'):
            raise ValueError('The official server did not return a PEM certificate')
        ssl.create_default_context(cadata=certificate)
        certificates.append(certificate)
    directory = BASE_DIR / 'certs'
    directory.mkdir(exist_ok=True)
    target = directory / 'max-ca.pem'
    temporary = target.with_suffix('.tmp')
    temporary.write_text('\n'.join(certificates) + '\n', encoding='ascii')
    ssl.create_default_context(cafile=str(temporary))
    temporary.replace(target)
    print('Project MAX CA bundle is ready:', target)


if __name__ == '__main__':
    main()
