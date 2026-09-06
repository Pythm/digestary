import os
import sys

# make `app/` importable as `server` / `init_db`
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..")
if APP not in sys.path:
    sys.path.insert(0, APP)
