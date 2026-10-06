"""اختبارات وسائط المساعد: ضغط صور الرؤية وتغليف WAV."""
import unittest

from daftari.ai.media import compress_for_vision, mime_of, pcm16_to_wav
from daftari.tests.test_motion_images_docs import _jpeg


class MediaTests(unittest.TestCase):
    def test_mime_and_wav_header(self):
        self.assertEqual(mime_of("a.JPG", b""), "image/jpeg")
        self.assertEqual(mime_of("x.wav"), "audio/wav")
        wav = pcm16_to_wav(b"\x00\x01" * 8, sample_rate=16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertGreater(len(wav), 44)

    def test_compress_for_vision_stays_jpeg(self):
        out = compress_for_vision(_jpeg((10, 80, 200), (1600, 1200)))
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        self.assertLess(len(out), 1600 * 1200)


if __name__ == "__main__":
    unittest.main()
