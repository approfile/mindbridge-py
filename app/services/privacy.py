import re


class PrivacySanitizer:
    patterns = [
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        re.compile(r"(?<![0-9A-Za-z])(?:\+?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?![0-9A-Za-z])"),
        re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ]
    
    def sanitize(self, text: str) -> str:
        sanitized = text or ""
        for pattern in self.patterns:
            sanitized = pattern.sub("[已脱敏]", sanitized)
        return sanitized

