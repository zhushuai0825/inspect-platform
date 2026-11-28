"""Admin-only publish log for this server.

The public inspect app must not import this package. It targets Python 3.11
and is loaded by the loopback admin process only when that process is 3.11
or newer.
"""

__version__ = "0.1.0"
