import hashlib
import lzma
from pathlib import Path
import tempfile
import unittest

from home_stack.build import layout, partition_script, checksum_text
from home_stack.catalog import entry
from home_stack.targets import validate_elf, validate_container


class ImageTests(unittest.TestCase):
    def table(self, label='gpt'):
        return {'label': label, 'sectorsize': 512, 'partitions': [
            {'node': 'image1', 'start': 2048, 'size': 65536, 'type': '83'},
            {'node': 'image2', 'start': 67584, 'size': 1400000, 'type': '83'}]}

    def test_partition_growth_preserves_boot_identity(self):
        table = self.table()
        n, size, start, seed_size = layout(table, 2048, 512)
        self.assertEqual(n, 2)
        self.assertEqual(start + seed_size, 2047 * 2048)
        self.assertEqual(size + table['partitions'][-1]['start'], start)
        dump = 'label: gpt\nlast-lba: 100\nimage1 : start=2048, size=65536, uuid=boot-id\nimage2 : start=67584, size=1400000, uuid=root-id\n'
        script = partition_script(dump, table, 2048, 512)
        self.assertIn('uuid=boot-id', script)
        self.assertIn('uuid=root-id', script)
        self.assertIn('image3 :', script)
        self.assertNotIn('last-lba', script)
        with self.assertRaises(ValueError):
            layout(table, 1024, 512)

    def test_rejects_overlap_and_wrong_architecture(self):
        table = self.table()
        table['partitions'][1]['start'] = 2000
        with self.assertRaises(ValueError):
            layout(table, 2048, 512)
        with self.assertRaises(ValueError):
            validate_elf(b'\x7fELF\x01\x01' + b'\0' * 12 + b'\x28\x00')
        validate_elf(b'\x7fELF\x02\x01' + b'\0' * 12 + b'\xb7\x00')
        validate_container({'architecture': 'arm64', 'os': 'linux'})
        with self.assertRaises(ValueError):
            validate_container({'architecture': 'amd64', 'os': 'linux'})

    def test_checksum_and_catalogue_use_uncompressed_hash(self):
        digest = hashlib.sha256(b'fixture' * 200).hexdigest()
        self.assertEqual(checksum_text(digest + '  base.img.xz'), digest)
        with self.assertRaises(ValueError):
            checksum_text(digest + ' ' + digest)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'test.img.xz'
            image.write_bytes(lzma.compress(b'fixture' * 200))
            data = entry(image, 'https://example.test/test.img.xz', '2026-10-24', 'https://example.test/icon.png')
            self.assertEqual(data['extract_size'], 1400)
            self.assertEqual(data['extract_sha256'], digest)
            self.assertEqual(data['image_download_size'], image.stat().st_size)
            self.assertNotEqual(data['image_download_size'], data['extract_size'])
            with self.assertRaises(ValueError):
                entry(image, 'http://192.0.2.1/test.img.xz', '2026-10-24', 'http://192.0.2.1/icon.png')
            local = entry(image, 'http://192.0.2.1/test.img.xz', '2026-10-24', 'http://192.0.2.1/icon.png', allow_http=True)
            self.assertEqual(local['extract_sha256'], digest)
