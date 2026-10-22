"""Pixel-crop checks; no API or CAD renderer required."""
from pathlib import Path
import os
import tempfile
import unittest
from PIL import Image
from benchcad_cadquery.inspection import crop_image


class CropTests(unittest.TestCase):
    def test_multiple_default_crops_preserve_every_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                image = Image.new('RGB', (30, 10), 'white')
                image.paste('red', (0, 0, 10, 10))
                image.paste('blue', (10, 0, 20, 10))
                image.save('input.png')
                boxes = [(0,0,10,10), (10,0,20,10), (20,0,30,10)]
                results = [crop_image('input.png', box) for box in boxes]
                self.assertEqual(len({r['png'] for r in results}), 3)
                for result, color in zip(results, [(255,0,0),(0,0,255),(255,255,255)]):
                    with Image.open(result['png']) as crop:
                        self.assertEqual(crop.getpixel((0,0)), color)
                self.assertEqual(crop_image('input.png', boxes[0])['png'], results[0]['png'])
            finally:
                os.chdir(previous)

    def test_original_pixels_and_invalid_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'input.png', Path(tmp)/'output.png'
            image = Image.new('RGB', (12, 10), 'white')
            image.putpixel((4, 3), (12, 34, 56))
            image.save(source)
            result = crop_image(source, (4, 3, 9, 7), output)
            self.assertEqual(result['size_pixels'], [5, 4])
            with Image.open(output) as crop:
                self.assertEqual(crop.getpixel((0, 0)), (12, 34, 56))
            for box in [(-1, 0, 5, 5), (0, 0, 99, 5), (2, 2, 1, 4), (0., 0, 2, 2)]:
                with self.subTest(box=box), self.assertRaises(ValueError):
                    crop_image(source, box, output)


if __name__ == '__main__':
    unittest.main()
