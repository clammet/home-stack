import http.client
import hashlib
import json
import lzma
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from home_stack.serve import byte_range, load_token, make_handler, resources


class ServerTests(unittest.TestCase):
    def test_ranges_and_unsatisfiable_inputs(self):
        self.assertEqual(byte_range('bytes=2-5', 10), (2, 5))
        self.assertEqual(byte_range('bytes=7-', 10), (7, 9))
        self.assertEqual(byte_range('bytes=-3', 10), (7, 9))
        self.assertEqual(byte_range('bytes=8-100', 10), (8, 9))
        for bad in ('bytes=11-', 'bytes=5-1', 'bytes=-0', 'bytes=0-1,4-5', 'not a range'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                byte_range(bad, 10)

    def test_token_is_private_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'token'
            token = load_token(path)
            self.assertEqual(token, load_token(path))
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_compressed_resume_validator_hashes_transferred_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'test.img.xz'
            compressed = lzma.compress(b'raw image contents' * 100)
            image.write_bytes(compressed)
            allowed = resources(image, 'http://127.0.0.1:8080/token', '2026-10-24')
            resource = allowed['/token/image.img.xz']
            self.assertEqual(resource.etag.strip('"'), hashlib.sha256(compressed).hexdigest())
            item = json.loads(allowed['/token/os_list.json'].content)['os_list'][0]
            self.assertNotEqual(resource.etag.strip('"'), item['extract_sha256'])

    def test_http_serves_only_allowed_resources_and_supports_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'test.img'
            image.write_bytes(b'0123456789')
            (Path(tmp) / 'secret.json').write_text('must not be served')
            allowed = resources(image, 'http://127.0.0.1:8080/test-token', '2026-10-24')
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(allowed))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, method='GET', headers=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    connection.request(method, path, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()
            try:
                status, headers, body = request('/test-token/os_list.json')
                self.assertEqual(status, 200)
                item = json.loads(body)['os_list'][0]
                self.assertEqual(item['extract_size'], 10)
                self.assertEqual(item['url'], 'http://127.0.0.1:8080/test-token/image.img')
                status, headers, body = request('/test-token/image.img', headers={'Range': 'bytes=3-6'})
                self.assertEqual((status, body), (206, b'3456'))
                self.assertEqual(headers['Content-Range'], 'bytes 3-6/10')
                status, headers, body = request('/test-token/image.img', 'HEAD')
                self.assertEqual((status, body, headers['Content-Length']), (200, b'', '10'))
                self.assertEqual(request('/test-token/image.img', headers={'Range': 'bytes=99-'})[0], 416)
                for path in ('/', '/secret.json', '/wrong-token/image.img', '/test-token/../secret.json', '/test-token/%2e%2e/secret.json'):
                    self.assertEqual(request(path)[0], 404)
                image.write_bytes(b'changed')
                self.assertEqual(request('/test-token/image.img')[0], 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
