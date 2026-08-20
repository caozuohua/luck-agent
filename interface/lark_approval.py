from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass


_DANGEROUS_REQUEST_RE = re.compile(
    r"(?:"
    r"重启|重置|停止|启动|部署|升级|回滚|删除|清理|恢复|备份|安装|卸载|推送|修改|写入|防火墙|杀进程"
    r"|\brestart\b|\breboot\b|\bstop\b|\bstart\b|\bdeploy\b|\bupgrade\b"
    r"|\brollback\b|\bdelete\b|\bremove\b|\brestore\b|\binstall\b|\buninstall\b"
    r"|\bpush\b|\bsudo\b|\bsystemctl\b|\bkill\b|\brm\b|\bgit\s+(?:commit|push|reset|clean)\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PendingApproval:
    token: str
    user_id: str
    request: str
    expires_at: float


class LarkApprovalManager:
    """Short-lived, one-use confirmations for potentially mutating requests."""

    def __init__(self, *, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = max(30.0, ttl_seconds)
        self._pending: dict[tuple[str, str], PendingApproval] = {}
        self._approved: dict[tuple[str, str], PendingApproval] = {}

    def requires_confirmation(self, text: str) -> bool:
        normalized = " ".join(text.strip().split())
        if not normalized or normalized.lower().startswith(("/confirm", "confirm ", "/cancel", "cancel")):
            return False
        return bool(_DANGEROUS_REQUEST_RE.search(normalized))

    def issue(self, *, user_id: str, request: str) -> PendingApproval:
        self._purge()
        token = secrets.token_urlsafe(6)
        pending = PendingApproval(
            token=token,
            user_id=user_id,
            request=request.strip(),
            expires_at=time.time() + self.ttl_seconds,
        )
        self._pending[(user_id, token.lower())] = pending
        return pending

    def confirm(self, *, user_id: str, token: str) -> PendingApproval | None:
        self._purge()
        key = (user_id, token.strip().lower())
        pending = self._pending.pop(key, None)
        if pending is not None:
            self._approved[key] = pending
        return pending

    def consume_grant(
        self,
        user_id: str,
        token: str,
        tool_name: str = "",
        args: dict[str, object] | None = None,
    ) -> bool:
        """Consume the grant exactly once at the tool execution boundary.

        The tool and args are accepted so the checker can later enforce a
        request-to-operation binding without changing the executor contract.
        The current grant is intentionally scoped to one approved request.
        """
        del tool_name, args
        self._purge()
        return self._approved.pop((user_id, token.strip().lower()), None) is not None

    def cancel(self, *, user_id: str) -> bool:
        removed = False
        for key in list(self._pending) + list(self._approved):
            if key[0] == user_id:
                self._pending.pop(key, None)
                self._approved.pop(key, None)
                removed = True
        return removed

    def _purge(self) -> None:
        now = time.time()
        for key, pending in list(self._pending.items()):
            if pending.expires_at <= now:
                self._pending.pop(key, None)
        for key, pending in list(self._approved.items()):
            if pending.expires_at <= now:
                self._approved.pop(key, None)
