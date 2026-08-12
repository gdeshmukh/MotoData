"""MotoData launcher -- opens the PyQt6 + pyqtgraph telemetry viewer.

    python viewer.py [folder]

Pass a WinTAX data root, a session, or a single car folder; or set
MOTODATA_ROOT. The last-opened folder is remembered between runs.
"""
from motodata.app import main

if __name__ == "__main__":
    main()
