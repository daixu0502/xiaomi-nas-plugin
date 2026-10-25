"""Export the raster master without regenerating an older icon."""
from pathlib import Path
import sys

source=Path(__file__).with_name('mediaeditor-master.png')
data=source.read_bytes()
assert data[:8]==b'\x89PNG\r\n\x1a\n' and data[16:24]==(300).to_bytes(4,'big')*2 and data[25]==6
Path(sys.argv[1]).write_bytes(data)
