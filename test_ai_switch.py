import tempfile, unittest
from pathlib import Path
import ai_switch

class SwitchTest(unittest.TestCase):
  def test_profile_name(self):
    with self.assertRaises(ValueError): ai_switch.profile('../bad')
  def test_atomic(self):
    with tempfile.TemporaryDirectory() as t:
      p=Path(t)/'x'; ai_switch.write_atomic(p,'secret'); self.assertEqual(p.read_text(),'secret'); self.assertEqual(p.stat().st_mode & 0o777,0o600)
if __name__=='__main__': unittest.main()
