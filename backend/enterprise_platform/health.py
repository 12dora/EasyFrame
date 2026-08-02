"""共享上游健康状态安全处理。"""

SENSITIVE_SUMMARY_MARKERS = (
    "secret",
    "token",
    "password",
    "authorization",
    "bearer",
    "密钥",
    "口令",
    "凭据",
)
SENSITIVE_SUMMARY_PLACEHOLDER = "[已隐藏敏感摘要]"


def safe_health_summary(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in SENSITIVE_SUMMARY_MARKERS):
        return SENSITIVE_SUMMARY_PLACEHOLDER
    return text[:500]
