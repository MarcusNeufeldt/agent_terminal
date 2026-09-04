"""Launch the Kraken Futures trading terminal.

Usage:
    python run.py            # http://127.0.0.1:8787
    PORT=9000 python run.py
"""

import server

if __name__ == "__main__":
    server.main()
