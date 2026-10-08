import sys
import socket
import ssl
import re
from urllib.parse import urlparse, urljoin

# max number of redirects to follow for a single URL
MAX_REDIRECTS = 5

# socket timeout in seconds
TIMEOUT = 5


def fetch(url):
    """Fetch a URL using a raw socket.

    Returns (status_code, reason, headers, body) on success,
    or None if a network error occurred.
    """
    parsed = urlparse(url)

    protocol = parsed.scheme
    host = parsed.hostname

    # default ports: 80 for http, 443 for https
    if protocol == 'http':
        port = parsed.port or 80
    elif protocol == 'https':
        port = parsed.port or 443
    else:
        return None

    if not host:
        return None

    # path must include the query string, if any
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query

    sock = None
    try:
        # create client socket, connect to server
        sock = socket.create_connection((host, port), timeout=TIMEOUT)

        # wrap the socket with TLS for https
        if protocol == 'https':
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)

        # send HTTP request
        request = f'GET {path} HTTP/1.1\r\n'
        request += f'Host: {host}\r\n'
        request += 'User-Agent: monitor\r\n'
        request += 'Accept: */*\r\n'
        request += 'Connection: close\r\n'
        request += '\r\n'
        sock.sendall(request.encode('utf-8'))

        # receive HTTP response until server closes the connection
        response = b''
        while True:
            data = sock.recv(4096)
            if not data:
                break
            response += data

    # connection, timeout, or TLS failure -> network error
    except (OSError, ssl.SSLError):
        return None

    finally:
        if sock:
            sock.close()

    return parse_response(response)


def parse_response(response):
    """Split a raw HTTP response into status, headers, and body."""
    # headers and body are separated by a blank line
    header_bytes, sep, body = response.partition(b'\r\n\r\n')
    if not sep:
        return None

    lines = header_bytes.decode('iso-8859-1').split('\r\n')

    # status line: HTTP-version SP status-code SP reason-phrase
    parts = lines[0].split(' ', 2)
    if len(parts) < 2 or not parts[0].startswith('HTTP/') or not parts[1].isdigit():
        return None

    code = int(parts[1])
    reason = parts[2] if len(parts) > 2 else ''

    # header names are case-insensitive
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(':')
        if colon:
            headers[name.strip().lower()] = value.strip()

    # reassemble body if sent in chunks
    if 'chunked' in headers.get('transfer-encoding', '').lower():
        body = dechunk(body)

    return code, reason, headers, body


def dechunk(body):
    """Decode a body sent with Transfer-Encoding: chunked."""
    result = b''
    while body:
        size_line, sep, body = body.partition(b'\r\n')
        if not sep:
            break
        try:
            # chunk size is in hex
            size = int(size_line.split(b';')[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        result += body[:size]
        body = body[size + 2:]  # skip trailing CRLF
    return result


def find_images(html, base_url):
    """Return absolute URLs of images referenced by <img src=...> tags."""
    images = []
    for tag in re.findall(r'<img\b[^>]*>', html, re.IGNORECASE):
        # src value may be double-quoted, single-quoted, or unquoted
        match = re.search(r'\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.IGNORECASE)
        if not match:
            continue
        src = (match.group(1) or match.group(2) or match.group(3) or '').strip()
        if not src or src.startswith('data:'):
            continue
        # resolve relative paths against the page URL
        image_url = urljoin(base_url, src)
        if image_url not in images:
            images.append(image_url)
    return images


def print_status(result):
    """Print the status line, or Network Error if the fetch failed."""
    if result is None:
        print('Status: Network Error')
    else:
        code, reason, _, _ = result
        print(f'Status: {code} {reason}'.rstrip())


def monitor(url):
    """Fetch a URL and print its status, redirects, and referenced images."""
    print(f'URL: {url}')
    result = fetch(url)
    print_status(result)

    # follow 301/302 redirections
    redirects = 0
    while result and result[0] in (301, 302) and redirects < MAX_REDIRECTS:
        location = result[2].get('location')
        if not location:
            break
        url = urljoin(url, location)
        print(f'Redirected URL: {url}')
        result = fetch(url)
        print_status(result)
        redirects += 1

    # fetch images referenced by a successfully returned HTML page
    # (only for the original URL, matching the expected sample output)
    if result and 200 <= result[0] < 300 and redirects == 0:
        content_type = result[2].get('content-type', '').lower()
        if 'html' in content_type:
            html = result[3].decode('utf-8', errors='ignore')
            for image_url in find_images(html, url):
                print(f'Referenced URL: {image_url}')
                print_status(fetch(image_url))

    print()


# get urls_file name from command line
if len(sys.argv) != 2:
    print('Usage: monitor urls_file')
    sys.exit()

