"""本地同步状态。

记录每周上次同步了多少节课、都是哪些 UID。这份快照是「非空守卫」的基线：
某周此前有课、这次抓到 0 条时，就能判定是异常而非学校撤课，从而拒绝删除。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class WeekRecord:
    """某一周的同步快照。"""

    count: int = 0
    uids: list[str] = field(default_factory=list)
    synced_at: str = ""

    def to_dict(self) -> dict:
        return {"count": self.count, "uids": sorted(self.uids), "synced_at": self.synced_at}

    @classmethod
    def from_dict(cls, raw: dict) -> WeekRecord:
        return cls(
            count=int(raw.get("count", 0)),
            uids=list(raw.get("uids", [])),
            synced_at=str(raw.get("synced_at", "")),
        )


@dataclass
class SyncState:
    """全量同步状态。周键为该周周一的 ISO 日期。"""

    weeks: dict[str, WeekRecord] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> SyncState:
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 状态文件坏了不该阻断同步：退回空状态，非空守卫会因此更保守。
            log.warning("状态文件读不出来（%s），按空状态处理", exc)
            return cls(path=path)

        weeks = {
            k: WeekRecord.from_dict(v)
            for k, v in (raw.get("weeks") or {}).items()
            if isinstance(v, dict)
        }
        return cls(weeks=weeks, path=path)

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("没有可写入的状态文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "weeks": {k: v.to_dict() for k, v in sorted(self.weeks.items())},
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def week_record(self, monday: date) -> WeekRecord:
        return self.weeks.get(monday.isoformat(), WeekRecord())

    def update_week(self, monday: date, uids: list[str]) -> None:
        self.weeks[monday.isoformat()] = WeekRecord(
            count=len(uids),
            uids=sorted(uids),
            synced_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def prune(self, before: date) -> int:
        """丢掉 before 之前的周记录，别让状态文件无限长。"""
        cutoff = before.isoformat()
        stale = [k for k in self.weeks if k < cutoff]
        for k in stale:
            del self.weeks[k]
        return len(stale)
