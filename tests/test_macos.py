import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from home_stack.config import load
from home_stack.macos import publish, source_archive

EXAMPLE = Path(__file__).resolve().parents[1] / 'config.example.json'


class MacTransferTests(unittest.TestCase):
    def test_archive_contains_only_inputs_and_private_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar'
            source_archive(path, load(EXAMPLE))
            with tarfile.open(path) as archive:
                names = archive.getnames()
                self.assertIn('home_stack/build.py', names)
                self.assertIn('runtime/controller.py', names)
                self.assertIn('config.json', names)
                self.assertEqual(archive.getmember('config.json').mode, 0o600)
                self.assertFalse(any(name.startswith(('build/', 'dist/', '.git/')) for name in names))
                self.assertFalse(any('__pycache__' in name for name in names))

    def fixture(self, path):
        image = path / 'image.img'
        image.write_bytes(b'test image')
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        (path / 'image.img.json').write_text(json.dumps({'image_sha256': digest}))
        (path / 'image.img.sha256').write_text(digest + '  image.img\n')

    def test_corrupt_transfer_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / 'image.img').write_bytes(b'corrupt')
            output = root / 'final.img'
            with self.assertRaises(ValueError):
                publish(root, output)
            self.assertFalse(output.exists())

    def test_publication_never_overwrites_or_leaves_partial_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            output = root / 'final.img'
            output.write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                publish(root, output)
            self.assertEqual(output.read_bytes(), b'existing')
            self.assertFalse(output.with_suffix('.img.sha256').exists())
            self.assertFalse(output.with_suffix('.img.json').exists())
