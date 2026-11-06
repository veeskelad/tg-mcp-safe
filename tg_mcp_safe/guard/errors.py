class GuardError(Exception):
    """Отказ стража. code — машинный код, reason — причина без значений секретов."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
