"""Legacy entry point for the MAX client."""
from assistant_app.channels.max_client import MAXBotClient, main, message_id, message_text

__all__ = ['MAXBotClient', 'message_id', 'message_text']

if __name__ == '__main__':
    main()
