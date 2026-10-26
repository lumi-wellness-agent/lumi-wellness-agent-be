"""검수된 활동 콘텐츠를 외부 공급자 또는 내부 자산에서 준비한다."""

from app.content.resolver import ContentResolution, resolve_activity_assets

__all__ = ["ContentResolution", "resolve_activity_assets"]
