"""Legacy entry point for the interactive MAX client."""
from assistant_app.channels.max_cli import MAXBotClientV2, main

__all__ = ['MAXBotClientV2']

if __name__ == '__main__':
    main()
