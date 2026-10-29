"""Read-only internet tools for the local model (no credentials or browser profile)."""
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from ddgs import DDGS
from ddgs.exceptions import DDGSException
import urllib3

from config import MAX_CA_BUNDLE, WEB_TIMEOUT, WEB_MAX_BYTES, WEB_MAX_CHARS



def public_target(url):
    if not isinstance(url, str) or len(url) > 4000:
        raise ValueError('Invalid URL')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only public http:// and https:// URLs without credentials are supported')
    host = parts.hostname.encode('idna').decode('ascii')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('Only public internet sites are accessible')
    return parts, host, port, ips[0]


def download_page(url):
    """Pin the validated public IP; keep TLS hostname checks and validate redirects."""
    for _ in range(6):
        parts, host, port, ip = public_target(url)
        kwargs = {'host': ip, 'port': port, 'timeout': urllib3.Timeout(connect=10, read=WEB_TIMEOUT),
                  'maxsize': 1, 'retries': False}
        if parts.scheme == 'https':
            import requests
            kwargs.update(cert_reqs='CERT_REQUIRED', server_hostname=host, assert_hostname=host,
                          ca_certs=MAX_CA_BUNDLE if isinstance(MAX_CA_BUNDLE, str) else requests.certs.where())
            pool = urllib3.HTTPSConnectionPool(**kwargs)
        else:
            pool = urllib3.HTTPConnectionPool(**kwargs)
        path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
        authority = f'[{host}]' if ':' in host else host
        if port != (443 if parts.scheme == 'https' else 80):
            authority += f':{port}'
        response = None
        try:
            response = pool.request('GET', path, headers={'Host': authority,
                                    'User-Agent': 'MAX-Assistant/1.0 (web reader)',
                                    'Accept': 'text/html,text/plain,application/json;q=0.9'},
                                    redirect=False, preload_content=False)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('Redirect has no target')
                url = urljoin(url, location)
                continue
            if response.status >= 400:
                raise ValueError(f'Website returned HTTP {response.status}')
            content_type = response.headers.get('Content-Type', '').lower()
            if content_type and not any(kind in content_type for kind in ('text/', 'html', 'json', 'xml')):
                raise ValueError('This tool reads text and HTML pages, not binary downloads')
            chunks = []
            size = 0
            deadline = time.monotonic() + WEB_TIMEOUT
            for chunk in response.stream(16384, decode_content=True):
                size += len(chunk)
                if size > WEB_MAX_BYTES:
                    raise ValueError('Page is too large; open a smaller page or use search')
                if time.monotonic() > deadline:
                    raise ValueError('Website read timed out')
                chunks.append(chunk)
            return url, b''.join(chunks)
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise ValueError('Too many redirects')


def web_search(query: str, max_results: int = 5) -> dict:
    """Search the public web. Returns titles, URLs and snippets; open pages to verify details."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        return {'error': 'Provide a search query of 1 to 500 characters'}
    try:
        count = max(1, min(8, int(max_results)))
        rows = DDGS(timeout=WEB_TIMEOUT).text(query.strip(), max_results=count, backend='auto')
        return {'query': query, 'results': [
            {'title': str(row.get('title', ''))[:300], 'url': str(row.get('href', ''))[:2000],
             'snippet': str(row.get('body', ''))[:1000]} for row in rows[:count]],
            'untrusted_content': True}
    except (DDGSException, ValueError, TypeError):
        return {'error': 'Search is temporarily unavailable. Try another query or open a known source URL.'}


def open_url(url: str) -> dict:
    """Open a public website and read its text and links. Does not execute JavaScript or sign in."""
    try:
        final_url, content = download_page(url)
        soup = BeautifulSoup(content, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        for tag in soup(['script', 'style', 'noscript', 'nav', 'footer', 'header', 'svg']):
            tag.decompose()
        links = []
        seen = set()
        for a in soup.find_all('a', href=True):
            target = urljoin(final_url, a['href'])
            if urlsplit(target).scheme in ('http', 'https') and target not in seen:
                seen.add(target)
                links.append({'text': a.get_text(' ', strip=True)[:160], 'url': target})
            if len(links) >= 30:
                break
        main = soup.find('main') or soup.find('article') or soup
        text = '\n'.join(line.strip() for line in main.get_text('\n', strip=True).splitlines() if line.strip())
        return {'url': final_url, 'title': title, 'text': text[:WEB_MAX_CHARS],
                'truncated': len(text) > WEB_MAX_CHARS, 'links': links, 'untrusted_content': True}
    except (ValueError, OSError, urllib3.exceptions.HTTPError):
        return {'url': url, 'error': 'Could not read this public page (network, HTTP, TLS or URL restriction). Try another source.'}


def execute_tool(name, arguments):
    if name not in ('web_search', 'open_url'):
        return {'error': 'Unknown web tool'}
    try:
        return globals()[name](**arguments)
    except (TypeError, ValueError):
        return {'error': 'Invalid tool arguments'}


TOOL_SCHEMAS = [
    {'type': 'function', 'function': {
        'name': 'web_search', 'description': 'Search the web for current information. Returns URLs and snippets.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'},
                       'max_results': {'type': 'integer'}}, 'required': ['query']}}},
    {'type': 'function', 'function': {
        'name': 'open_url', 'description': 'Open a public URL and read website text and links.',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}}},
]

