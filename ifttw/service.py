"""Android foreground service: runs the local server and the NASA/battery monitor in the background."""
import os
import sys
import traceback

try:                                                   # make sure the project root (which contains ifttw/) is importable
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    _root = os.getcwd()
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from ifttw import app
    app.run()
except BaseException:
    from ifttw import errpage
    errpage.show('service', traceback.format_exc())
