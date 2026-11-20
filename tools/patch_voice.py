"""Patch core/voice.py: load the speech package (piper) only when Cerry first speaks.

Run once, from any folder:   python tools/patch_voice.py
It makes core/voice.py.bak, checks that core/voice.py is the expected version, and changes two places.
Running it again does nothing. To undo: copy core/voice.py.bak over core/voice.py.
"""
import shutil
import sys
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "core" / "voice.py"
if not path.exists():
    sys.exit("core/voice.py not found - run this from the Cherry project.")
text = path.read_bytes().decode("utf-8")
crlf = "\r\n" in text
text = text.replace("\r\n", "\n")

OLD_IMPORT = "try:\n    from piper import SynthesisConfig\nexcept Exception:\n    SynthesisConfig = None\n"
NEW_IMPORT = "SynthesisConfig = None          # imported on first use (see Speaker._synth)\n"
OLD_SYNTH = "    def _synth(self, lang, text):\n        import numpy as np\n"
NEW_SYNTH = ("    def _synth(self, lang, text):\n        import numpy as np\n"
             "        global SynthesisConfig\n        if SynthesisConfig is None:\n            try:\n"
             "                from piper import SynthesisConfig as _sc\n                SynthesisConfig = _sc\n"
             "            except Exception:\n                pass\n")

if NEW_IMPORT in text:
    sys.exit("voice.py is already patched - nothing to do.")
if OLD_IMPORT not in text or OLD_SYNTH not in text:
    sys.exit("voice.py is not the expected version - nothing was changed.")
backup = path.with_suffix(path.suffix + ".bak")
shutil.copy2(path, backup)
text = text.replace(OLD_IMPORT, NEW_IMPORT, 1).replace(OLD_SYNTH, NEW_SYNTH, 1)
path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
print("voice.py patched. Backup: " + str(backup))
