class TelegramAPIError(Exception):
    pass
class TelegramRetryAfter(TelegramAPIError):
    retry_after = 1
