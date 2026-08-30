"""Meeting lifecycle, ingestion, durable analysis workers, and finalization."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .openai_gateway import OpenAIGateway
from .schemas import MinutesDraft, ProgressAnalysis, TranscriptFinal
from .storage import SQLiteStore, utc_now


EventSink = Callable[[dict], Awaitable[None]]

_FACT_PATTERN = re.compile(
    r"(?:\d[\d,.]*\s*(?:%|퍼센트|원|만원|억원|달러|명|건|초|분|시간|일|월|년|GB|MB|ms|s)?)"
    r"|(?:출시|발표|통계|기준금리|법률|규정|점유율|성능|벤치마크|매출|계약|예산)",
    re.IGNORECASE,
)
_INTERNAL_MARKERS = (
    "우리 ",
    "저희 ",
    "사내",
    "내부",
    "고객사",
    "프로젝트",
    "이번 분기 매출",
    "계약 금액",
    "우리 팀",
    "예산",
)
_AGREEMENT_MARKERS = ("결정", "확정", "합의", "채택", "진행하죠", "하기로", "그렇게 하죠")
_PROGRESS_TIMEOUT_S = 45.0
_FACT_TIMEOUT_S = 60.0
_MINUTES_TIMEOUT_S = 180.0
_DIARIZATION_TIMEOUT_S = 300.0


class MeetingService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        gateway: OpenAIGateway,
        audio_dir: str | Path,
        event_sink: EventSink | None = None,
        progress_interval_s: float | None = None,
        fact_workers: int = 2,
    ):
        self.store = store
        self.gateway = gateway
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.event_sink = event_sink or _discard_event
        self.progress_interval_s = float(
            progress_interval_s
            if progress_interval_s is not None
            else os.getenv("GOOD_LISTENER_PROGRESS_INTERVAL_S", "20")
        )
        self.fact_worker_count = max(1, min(int(fact_workers), 4))
        self.finalize_fact_wait_s = max(
            0.0, float(os.getenv("GOOD_LISTENER_FINALIZE_FACT_WAIT_S", "20"))
        )
        self._signals = {
            "progress": asyncio.Queue(maxsize=16),
            "fact": asyncio.Queue(maxsize=32),
            "finalize": asyncio.Queue(maxsize=8),
        }
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._last_progress_at: dict[str, float] = {}

    async def start_workers(self) -> None:
        if self._tasks:
            return
        self._stopping.clear()
        self.store.requeue_expired_jobs()
        self._tasks.append(asyncio.create_task(self._worker_loop("progress")))
        self._tasks.extend(
            asyncio.create_task(self._worker_loop("fact"))
            for _ in range(self.fact_worker_count)
        )
        self._tasks.append(asyncio.create_task(self._worker_loop("finalize")))
        for kind in self._signals:
            self.wake(kind)

    async def stop_workers(self) -> None:
        self._stopping.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def wake(self, kind: str) -> None:
        queue = self._signals[kind]
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            # The SQLite job is durable; a pending wake-up is enough.
            pass

    def create_meeting(
        self,
        *,
        topic: str,
        goal: str,
        terms: list[str],
        consent_external_processing: bool,
    ) -> dict:
        meeting = self.store.create_meeting(
            topic=topic,
            goal=goal,
            terms=terms,
            consent_external_processing=consent_external_processing,
        )
        return self.snapshot(meeting["id"])

    async def start_meeting(self, meeting_id: str) -> dict:
        meeting, event = self.store.transition(meeting_id, "live")
        await self._emit(event)
        return self.snapshot(meeting_id)

    async def pause_meeting(self, meeting_id: str) -> dict:
        _, event = self.store.transition(meeting_id, "paused")
        await self._emit(event)
        return self.snapshot(meeting_id)

    async def resume_meeting(self, meeting_id: str) -> dict:
        _, event = self.store.transition(meeting_id, "live")
        await self._emit(event)
        return self.snapshot(meeting_id)

    async def stop_meeting(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        event = None
        if meeting["lifecycle"] in {"live", "paused"}:
            meeting, event = self.store.transition(meeting_id, "finalizing")
            await self._emit(event)
        if meeting["lifecycle"] in {"finalizing", "failed"}:
            if meeting["lifecycle"] == "failed":
                meeting, event = self.store.transition(meeting_id, "finalizing")
                await self._emit(event)
            job, _ = self._enqueue_finalize_job(meeting, retry=False)
            if job["status"] in {"failed", "succeeded"} and self.store.get_minutes(meeting_id) is None:
                # A retry after failure receives a new durable key.
                self._enqueue_finalize_job(meeting, retry=True)
            self.wake("finalize")
        return self.snapshot(meeting_id)

    async def retry_finalization(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting["lifecycle"] not in {"review", "failed"}:
            raise ValueError("finalization retry is available only from review or failed state")
        meeting, event = self.store.transition(meeting_id, "finalizing")
        await self._emit(event)
        self._enqueue_finalize_job(meeting, retry=True)
        self.wake("finalize")
        return self.snapshot(meeting_id)

    async def approve_minutes(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting["lifecycle"] != "review":
            raise ValueError("minutes can be approved only from review state")
        minutes = self.store.get_minutes(meeting_id)
        if minutes is None or minutes["transcript_revision"] != meeting["transcript_revision"]:
            raise ValueError("minutes are not current for this transcript")
        if not self._minutes_sources_current(meeting_id, minutes):
            raise ValueError("minutes are stale because meeting sources changed")
        minutes, event = self.store.approve_minutes(meeting_id)
        await self._emit(event)
        _, lifecycle_event = self.store.transition(meeting_id, "completed")
        await self._emit(lifecycle_event)
        return minutes

    async def ingest_final(self, meeting_id: str, data: TranscriptFinal) -> tuple[dict, bool]:
        utterance, event, inserted = self.store.add_utterance(
            meeting_id,
            external_item_id=data.item_id,
            previous_item_id=data.previous_item_id,
            text=data.text,
            speaker=data.speaker,
            started_at=data.started_at,
            ended_at=data.ended_at,
        )
        await self._emit(event)
        if not inserted:
            return utterance, False

        meeting = self.store.get_meeting(meeting_id)
        self._schedule_progress(meeting, utterance)
        await self._schedule_fact_if_needed(meeting, utterance)
        return utterance, True

    async def persist_audio_chunk(
        self,
        meeting_id: str,
        *,
        chunk_id: str,
        sequence: int,
        content_type: str,
        content: bytes,
        expected_sha256: str = "",
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> tuple[dict, bool]:
        self.store.get_meeting(meeting_id)
        if not content:
            raise ValueError("audio chunk is empty")
        if len(content) > 32 * 1024 * 1024:
            raise ValueError("audio chunk exceeds 32 MiB")
        if sequence < 0:
            raise ValueError("audio sequence must be non-negative")
        if not chunk_id or len(chunk_id) > 200:
            raise ValueError("invalid chunk_id")
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 and not _constant_equal(digest, expected_sha256.lower()):
            raise ValueError("audio chunk sha256 mismatch")
        safe_chunk = re.sub(r"[^A-Za-z0-9_.-]", "_", chunk_id)[:160]
        if not safe_chunk:
            raise ValueError("invalid chunk_id")
        for existing in self.store.list_audio_metadata(meeting_id):
            if existing["chunk_id"] == chunk_id:
                if int(existing["sequence"]) != sequence or existing["sha256"] != digest:
                    raise ValueError("chunk_id already exists with different content")
                existing_path = Path(existing["path"])
                if not existing_path.exists():
                    existing_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_encrypted_audio(
                        existing_path,
                        self.store.cipher.encrypt_bytes(
                            content,
                            context=f"audio:{meeting_id}:{chunk_id}",
                        ),
                    )
                return self._audio_response_metadata(
                    meeting_id, existing, regeneration_started=False
                ), False
            if int(existing["sequence"]) == sequence:
                raise ValueError("audio sequence already belongs to another chunk")
        meeting_dir = self.audio_dir / meeting_id
        meeting_dir.mkdir(parents=True, exist_ok=True)
        path = meeting_dir / f"{int(sequence):08d}-{safe_chunk}.glenc"
        encrypted = self.store.cipher.encrypt_bytes(
            content,
            context=f"audio:{meeting_id}:{chunk_id}",
        )
        if not path.exists():
            _write_encrypted_audio(path, encrypted)
        metadata, event, inserted = self.store.add_audio_metadata(
            meeting_id,
            chunk_id=chunk_id,
            sequence=sequence,
            content_type=content_type,
            size_bytes=len(content),
            sha256=digest,
            path=str(path),
            started_at=started_at,
            ended_at=ended_at,
        )
        if not inserted and metadata.get("sha256") != digest:
            raise ValueError("chunk_id already exists with different content")
        await self._emit(event)
        regeneration_started = False
        if inserted:
            regeneration_started = await self._refresh_minutes_for_changed_sources(
                meeting_id, reason="audio_changed"
            )
        return self._audio_response_metadata(
            meeting_id, metadata, regeneration_started=regeneration_started
        ), inserted

    def _audio_response_metadata(
        self,
        meeting_id: str,
        metadata: dict,
        *,
        regeneration_started: bool,
    ) -> dict:
        current = self.store.get_meeting(meeting_id)
        minutes = self.store.get_minutes(meeting_id)
        return {
            **metadata,
            "lifecycle": current["lifecycle"],
            "minutes_stale": bool(
                minutes is not None
                and not self._minutes_sources_current(meeting_id, minutes)
            ),
            "regeneration_started": regeneration_started,
        }

    def snapshot(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        state_row = self.store.latest_snapshot(meeting_id)
        state = dict((state_row or {}).get("data") or {})
        state.setdefault("current_topic", "")
        state.setdefault("current_topic_evidence_ids", [])
        state.setdefault("progress", [])
        state.setdefault("decisions", [])
        state.setdefault("action_items", [])
        state.setdefault("open_questions", [])
        return {
            "meeting": meeting,
            "lifecycle": meeting["lifecycle"],
            "revision": meeting["revision"],
            "transcript_revision": meeting["transcript_revision"],
            "transcript": self._canonical_transcript(meeting_id),
            **state,
            "facts": self.store.list_claims(meeting_id),
            "minutes": self.store.get_minutes(meeting_id),
            "pending_jobs": self.store.count_pending_jobs(meeting_id),
            "external_processing": {
                "required": True,
                "consent_required": True,
                "provider": "OpenAI API",
            },
        }

    async def delete_meeting(self, meeting_id: str) -> None:
        self.store.get_meeting(meeting_id)
        paths = self.store.audio_paths(meeting_id)
        for raw_path in paths:
            path = Path(raw_path)
            path.unlink(missing_ok=True)
        meeting_dir = self.audio_dir / meeting_id
        if meeting_dir.exists():
            for orphan in meeting_dir.iterdir():
                if orphan.is_file():
                    orphan.unlink()
            meeting_dir.rmdir()
        self.store.delete_meeting(meeting_id)

    def _minutes_sources_current(self, meeting_id: str, minutes: dict | None = None) -> bool:
        minutes = minutes if minutes is not None else self.store.get_minutes(meeting_id)
        if minutes is None:
            return False
        meeting = self.store.get_meeting(meeting_id)
        source = (minutes.get("structured") or {}).get("_source") or {}
        return (
            int(minutes["transcript_revision"]) == int(meeting["transcript_revision"])
            and int(source.get("transcript_revision", -1))
            == int(meeting["transcript_revision"])
            and source.get("audio_manifest_sha256")
            == _audio_manifest_sha256(self.store.list_audio_metadata(meeting_id))
            and source.get("fact_manifest_sha256")
            == _fact_manifest_sha256(self.store.list_claims(meeting_id))
        )

    async def _refresh_minutes_for_changed_sources(
        self, meeting_id: str, *, reason: str
    ) -> bool:
        minutes = self.store.get_minutes(meeting_id)
        if minutes is None or self._minutes_sources_current(meeting_id, minutes):
            return False
        meeting = self.store.get_meeting(meeting_id)
        if meeting["lifecycle"] not in {"review", "completed", "finalizing"}:
            return False
        _, stale_event = self.store.mark_minutes_stale(meeting_id, reason=reason)
        await self._emit(stale_event)
        if meeting["lifecycle"] in {"review", "completed"}:
            meeting, lifecycle_event = self.store.transition(meeting_id, "finalizing")
            await self._emit(lifecycle_event)
        else:
            meeting = self.store.get_meeting(meeting_id)
        self._enqueue_finalize_job(meeting, retry=False)
        self.wake("finalize")
        return True

    def _schedule_progress(self, meeting: dict, utterance: dict) -> None:
        now = time.monotonic()
        last = self._last_progress_at.get(meeting["id"], 0.0)
        first = meeting["transcript_revision"] == 1
        checkpoint = meeting["transcript_revision"] % 5 == 0
        if not first and not checkpoint and now - last < self.progress_interval_s:
            return
        self._last_progress_at[meeting["id"]] = now
        self.store.enqueue_latest_progress(
            meeting["id"],
            base_revision=meeting["transcript_revision"],
            payload={"through_utterance_id": utterance["id"]},
        )
        self.wake("progress")

    def _enqueue_current_progress(self, meeting_id: str) -> None:
        meeting = self.store.get_meeting(meeting_id)
        if meeting["lifecycle"] not in {"live", "paused", "finalizing"}:
            return
        utterances = self.store.list_utterances(meeting_id)
        if not utterances:
            return
        self.store.enqueue_latest_progress(
            meeting_id,
            base_revision=meeting["transcript_revision"],
            payload={"through_utterance_id": utterances[-1]["id"]},
        )
        self.wake("progress")

    def _enqueue_finalize_job(self, meeting: dict, *, retry: bool) -> tuple[dict, bool]:
        audio_manifest = _audio_manifest_sha256(self.store.list_audio_metadata(meeting["id"]))
        fact_manifest = _fact_manifest_sha256(self.store.list_claims(meeting["id"]))
        source_key = (
            f"transcript:{meeting['transcript_revision']}:audio:{audio_manifest}:facts:{fact_manifest}"
        )
        dedupe_key = f"retry:{source_key}:{uuid.uuid4()}" if retry else source_key
        return self.store.enqueue_job(
            meeting["id"],
            kind="finalize",
            dedupe_key=dedupe_key,
            base_revision=meeting["transcript_revision"],
            payload={
                "requested_at": utc_now(),
                "retry": retry,
                "audio_manifest_sha256": audio_manifest,
                "fact_manifest_sha256": fact_manifest,
            },
        )

    async def _schedule_fact_if_needed(self, meeting: dict, utterance: dict) -> None:
        text = utterance["text"]
        if _FACT_PATTERN.search(text) is None:
            return
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        scope = "internal" if any(marker in text for marker in _INTERNAL_MARKERS) else "public"
        status = "internal_source_required" if scope == "internal" else "queued"
        claim, event, created = self.store.upsert_claim(
            meeting["id"],
            utterance_id=utterance["id"],
            normalized_hash=digest,
            claim_text=text,
            scope=scope,
            status=status,
            base_revision=meeting["revision"],
        )
        await self._emit(event)
        if not created or scope == "internal":
            return
        self.store.enqueue_job(
            meeting["id"],
            kind="fact",
            dedupe_key=digest,
            base_revision=meeting["revision"],
            payload={"claim_id": claim["id"]},
        )
        self.wake("fact")

    async def _worker_loop(self, kind: str) -> None:
        queue = self._signals[kind]
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(queue.get(), timeout=0.75)
            except asyncio.TimeoutError:
                pass
            while not self._stopping.is_set():
                job = self.store.claim_job(
                    [kind], lease_seconds=600.0 if kind == "finalize" else 90.0
                )
                if job is None:
                    break
                try:
                    if kind == "progress":
                        await self._run_progress(job)
                    elif kind == "fact":
                        await self._run_fact(job)
                    else:
                        await self._run_finalize(job)
                    self.store.complete_job(job["id"], job["lease_id"])
                except asyncio.CancelledError:
                    self.store.fail_job(
                        job["id"], job["lease_id"], "worker cancelled", retry_delay_s=0
                    )
                    raise
                except Exception as exc:
                    retry = min(30.0, 2.0 ** job["attempts"]) if job["attempts"] < 3 else None
                    self.store.fail_job(
                        job["id"],
                        job["lease_id"],
                        f"{type(exc).__name__}: {exc}",
                        retry_delay_s=retry,
                    )
                    if retry is None and kind == "fact":
                        claim_id = str(job["payload"].get("claim_id") or "")
                        try:
                            _, event = self.store.update_claim(
                                claim_id,
                                status="failed",
                                verdict="공개 웹 팩트 확인을 완료하지 못했습니다.",
                            )
                            await self._emit(event)
                            await self._refresh_minutes_for_changed_sources(
                                job["meeting_id"], reason="fact_changed"
                            )
                        except KeyError:
                            pass
                    if retry is None and kind == "finalize":
                        try:
                            _, event = self.store.transition(
                                job["meeting_id"], "failed", error=f"{type(exc).__name__}: {exc}"
                            )
                            await self._emit(event)
                        except (KeyError, ValueError):
                            pass

    async def _run_progress(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting["lifecycle"] not in {"live", "paused", "finalizing"}:
            return
        if meeting["transcript_revision"] != job["base_revision"]:
            self._enqueue_current_progress(meeting_id)
            return
        utterances = self.store.list_utterances(meeting_id)
        previous = self.store.latest_snapshot(meeting_id)
        parsed = await asyncio.wait_for(
            self.gateway.analyze_progress(
                meeting=meeting,
                utterances=utterances,
                previous_state=(previous or {}).get("data") or {},
            ),
            timeout=_PROGRESS_TIMEOUT_S,
        )
        if self.store.get_meeting(meeting_id)["transcript_revision"] != job["base_revision"]:
            self._enqueue_current_progress(meeting_id)
            return
        state = self._sanitize_progress(parsed, utterances)
        _, event, applied = self.store.save_snapshot_if_fresh(
            meeting_id,
            base_revision=job["base_revision"],
            data=state,
        )
        if applied:
            await self._emit(event)

    async def _run_fact(self, job: dict) -> None:
        claim_id = str(job["payload"].get("claim_id") or "")
        claims = self.store.list_claims(job["meeting_id"])
        claim = next((item for item in claims if item["id"] == claim_id), None)
        if (
            claim is None
            or claim["status"] == "internal_source_required"
            or claim["status"] not in {"queued", "searching"}
            or int(claim["base_revision"]) != int(job["base_revision"])
        ):
            return
        _, searching_event = self.store.update_claim(claim_id, status="searching")
        await self._emit(searching_event)
        utterances = self.store.list_utterances(job["meeting_id"])
        context = "\n".join(item["text"] for item in utterances[-10:])
        result = await asyncio.wait_for(
            self.gateway.verify_public_fact(
                claim=claim["claim_text"],
                context=context,
            ),
            timeout=_FACT_TIMEOUT_S,
        )
        current = next(
            (item for item in self.store.list_claims(job["meeting_id"]) if item["id"] == claim_id),
            None,
        )
        if (
            current is None
            or int(current["base_revision"]) != int(job["base_revision"])
            or current["status"] != "searching"
        ):
            return
        _, event = self.store.update_claim(
            claim_id,
            status=result.status,
            verdict=result.verdict,
            sources=[source.model_dump(mode="json") for source in result.sources],
        )
        await self._emit(event)
        await self._refresh_minutes_for_changed_sources(
            job["meeting_id"], reason="fact_changed"
        )

    async def _run_finalize(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        audio_manifest = _audio_manifest_sha256(self.store.list_audio_metadata(meeting_id))
        expected_audio_manifest = str(
            job["payload"].get("audio_manifest_sha256") or audio_manifest
        )
        if (
            meeting["transcript_revision"] != job["base_revision"]
            or audio_manifest != expected_audio_manifest
        ):
            self._enqueue_finalize_job(meeting, retry=False)
            self.wake("finalize")
            return
        existing = self.store.get_minutes(meeting_id)
        existing_source = ((existing or {}).get("structured") or {}).get("_source") or {}
        fact_manifest = _fact_manifest_sha256(self.store.list_claims(meeting_id))
        if (
            existing is not None
            and existing["transcript_revision"] == meeting["transcript_revision"]
            and existing_source.get("audio_manifest_sha256") == audio_manifest
            and existing_source.get("fact_manifest_sha256") == fact_manifest
            and not bool(job["payload"].get("retry"))
        ):
            if meeting["lifecycle"] == "finalizing":
                _, event = self.store.transition(meeting_id, "review")
                await self._emit(event)
            return

        deadline = asyncio.get_running_loop().time() + self.finalize_fact_wait_s
        while (
            self.store.count_pending_jobs(meeting_id, "fact") > 0
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.2)

        live_utterances = _order_realtime_items(
            [
                item
                for item in self.store.list_utterances(meeting_id)
                if not str(item.get("source") or "").startswith("diarized")
            ]
        )
        diarized, audio_warnings = await self._diarize_persisted_audio(meeting)
        if _audio_manifest_sha256(self.store.list_audio_metadata(meeting_id)) != audio_manifest:
            refreshed = self.store.get_meeting(meeting_id)
            self._enqueue_finalize_job(refreshed, retry=False)
            self.wake("finalize")
            return
        utterances = diarized or live_utterances
        input_meeting = self.store.get_meeting(meeting_id)
        input_transcript_revision = input_meeting["transcript_revision"]
        input_audio_manifest = audio_manifest
        snapshot_row = self.store.latest_snapshot(meeting_id)
        claims = self.store.list_claims(meeting_id)
        input_fact_manifest = _fact_manifest_sha256(claims)
        draft = await asyncio.wait_for(
            self.gateway.generate_minutes(
                meeting=meeting,
                utterances=utterances,
                snapshot=(snapshot_row or {}).get("data") or {},
                claims=claims,
            ),
            timeout=_MINUTES_TIMEOUT_S,
        )
        clean = self._sanitize_minutes(draft, utterances, claims)
        clean["_source"] = {
            "transcript_revision": input_transcript_revision,
            "audio_manifest_sha256": input_audio_manifest,
            "fact_manifest_sha256": input_fact_manifest,
        }
        clean.setdefault("warnings", []).extend(audio_warnings)
        if self.store.count_pending_jobs(meeting_id, "fact") > 0:
            clean["warnings"].append(
                "일부 팩트 확인이 완료되지 않아 회의록에는 근거 부족으로 표시했습니다."
            )
        refreshed = self.store.get_meeting(meeting_id)
        if (
            refreshed["transcript_revision"] != input_transcript_revision
            or _audio_manifest_sha256(self.store.list_audio_metadata(meeting_id))
            != input_audio_manifest
            or _fact_manifest_sha256(self.store.list_claims(meeting_id))
            != input_fact_manifest
        ):
            self._enqueue_finalize_job(refreshed, retry=False)
            self.wake("finalize")
            return
        markdown = render_minutes_markdown(clean)
        _, event = self.store.upsert_minutes(
            meeting_id,
            transcript_revision=refreshed["transcript_revision"],
            structured=clean,
            markdown=markdown,
        )
        await self._emit(event)
        _, lifecycle_event = self.store.transition(meeting_id, "review")
        await self._emit(lifecycle_event)

    async def _diarize_persisted_audio(self, meeting: dict) -> tuple[list[dict], list[str]]:
        metadata = self.store.list_audio_metadata(meeting["id"])
        if not metadata:
            return [], ["저장된 오디오가 없어 실시간 확정 자막으로 회의록을 작성했습니다."]
        sequences = [int(chunk["sequence"]) for chunk in metadata]
        if any(current != previous + 1 for previous, current in zip(sequences, sequences[1:])):
            return [], [
                "저장된 오디오 청크 순서에 누락이 있어 실시간 확정 자막으로 회의록을 작성했습니다."
            ]
        content_types = {
            str(chunk.get("content_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
            for chunk in metadata
        }
        if len(content_types) != 1:
            return [], [
                "저장된 오디오 형식이 서로 달라 실시간 확정 자막으로 회의록을 작성했습니다."
            ]

        try:
            parts = []
            for chunk in metadata:
                encrypted = Path(chunk["path"]).read_bytes()
                parts.append(
                    self.store.cipher.decrypt_bytes(
                        encrypted,
                        context=f"audio:{meeting['id']}:{chunk['chunk_id']}",
                    )
                )
            content_type = str(metadata[0].get("content_type") or "application/octet-stream")
            result = await asyncio.wait_for(
                self.gateway.transcribe_audio_chunk(
                    filename=f"meeting{_audio_extension(content_type)}",
                    content=b"".join(parts),
                    content_type=content_type,
                ),
                timeout=_DIARIZATION_TIMEOUT_S,
            )
            segments = result.get("segments") or []
            if not segments and str(result.get("text") or "").strip():
                segments = [{"text": result["text"], "speaker": "unknown"}]
            output: list[dict] = []
            manifest = _audio_manifest_sha256(metadata)
            run_id = uuid.uuid4().hex
            source = f"diarized:{manifest}:{run_id}"
            for index, segment in enumerate(segments):
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                item, _, _ = self.store.add_utterance(
                    meeting["id"],
                    external_item_id=f"{source}:{index}",
                    text=text,
                    speaker=str(segment.get("speaker") or "unknown"),
                    started_at=_seconds_label(segment.get("start")),
                    ended_at=_seconds_label(segment.get("end")),
                    source=source,
                )
                output.append(item)
            if output:
                return output, []
            return [], ["오디오 재전사 결과가 비어 있어 실시간 확정 자막을 사용했습니다."]
        except Exception as exc:
            return [], [
                f"오디오 결합 재전사 실패({type(exc).__name__})로 실시간 확정 자막을 사용했습니다."
            ]

    def _canonical_transcript(self, meeting_id: str) -> list[dict]:
        all_items = self.store.list_utterances(meeting_id)
        manifest = _audio_manifest_sha256(self.store.list_audio_metadata(meeting_id))
        prefix = f"diarized:{manifest}:"
        groups: dict[str, list[dict]] = {}
        for item in all_items:
            source = str(item.get("source") or "")
            if source.startswith(prefix):
                groups.setdefault(source, []).append(item)
        if groups:
            return max(groups.values(), key=lambda values: max(item["seq"] for item in values))

        minutes = self.store.get_minutes(meeting_id)
        source_manifest = (((minutes or {}).get("structured") or {}).get("_source") or {}).get(
            "audio_manifest_sha256"
        )
        legacy = [item for item in all_items if item.get("source") == "diarized"]
        if legacy and source_manifest == manifest:
            return legacy
        return _order_realtime_items(
            [
                item
                for item in all_items
                if not str(item.get("source") or "").startswith("diarized")
            ]
        )

    @staticmethod
    def _sanitize_progress(parsed: ProgressAnalysis, utterances: list[dict]) -> dict:
        data = parsed.model_dump(mode="json")
        allowed = {item["id"] for item in utterances}
        by_id = {item["id"]: item["text"] for item in utterances}
        data["current_topic_evidence_ids"] = _valid_evidence(
            data.get("current_topic_evidence_ids", []), allowed
        )
        if not data["current_topic_evidence_ids"]:
            data["current_topic"] = ""
        for key in ("progress", "decisions", "action_items", "open_questions"):
            clean = []
            for item in data.get(key, []):
                evidence = _valid_evidence(item.get("evidence_utterance_ids", []), allowed)
                if not evidence:
                    continue
                item["evidence_utterance_ids"] = evidence
                evidence_text = " ".join(by_id[item_id] for item_id in evidence)
                if key == "decisions" and item.get("status") == "confirmed":
                    if not any(marker in evidence_text for marker in _AGREEMENT_MARKERS):
                        item["status"] = "candidate"
                if key == "action_items":
                    if item.get("assignee") and str(item["assignee"]) not in evidence_text:
                        item["assignee"] = None
                    if item.get("due_at") and str(item["due_at"]) not in evidence_text:
                        item["due_at"] = None
                clean.append(item)
            data[key] = clean
        return data

    @staticmethod
    def _sanitize_minutes(
        draft: MinutesDraft,
        utterances: list[dict],
        claims: list[dict],
    ) -> dict:
        data = draft.model_dump(mode="json")
        allowed = {item["id"] for item in utterances}
        allowed_claims = {item["id"]: item for item in claims}
        for key in ("agenda", "decisions", "action_items", "open_questions"):
            clean = []
            for item in data.get(key, []):
                evidence = _valid_evidence(item.get("evidence_utterance_ids", []), allowed)
                if evidence:
                    item["evidence_utterance_ids"] = evidence
                    clean.append(item)
            data[key] = clean
        status_map = {
            "queued": "insufficient",
            "searching": "insufficient",
            "supported": "supported",
            "contradicted": "contradicted",
            "insufficient": "insufficient",
            "internal_source_required": "internal_source_required",
            "failed": "failed",
        }
        data["facts"] = [
            {
                "claim_id": source["id"],
                "claim": source["claim_text"],
                "status": status_map.get(source["status"], "insufficient"),
                "verdict": source["verdict"],
                "sources": source["sources"],
                "evidence_utterance_ids": [source["utterance_id"]],
            }
            for source in allowed_claims.values()
        ]
        return data

    async def _emit(self, event: dict | None) -> None:
        if event is not None:
            await self.event_sink(event)


async def _discard_event(event: dict) -> None:
    _ = event


def _valid_evidence(values: list[str], allowed: set[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value) in allowed))


def _order_realtime_items(items: list[dict]) -> list[dict]:
    """Reconcile out-of-order completion events using OpenAI item links."""
    if len(items) < 2:
        return items
    by_external = {item["external_item_id"]: item for item in items}
    children: dict[str | None, list[dict]] = {}
    for item in items:
        previous = item.get("previous_item_id")
        parent = previous if previous in by_external else None
        children.setdefault(parent, []).append(item)
    for values in children.values():
        values.sort(key=lambda item: item["seq"])
    ordered: list[dict] = []
    visited: set[str] = set()

    def visit(item: dict) -> None:
        external_id = item["external_item_id"]
        if external_id in visited:
            return
        visited.add(external_id)
        ordered.append(item)
        for child in children.get(external_id, []):
            visit(child)

    for root in children.get(None, []):
        visit(root)
    for item in sorted(items, key=lambda value: value["seq"]):
        visit(item)
    return ordered


def _constant_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _write_encrypted_audio(path: Path, encrypted: bytes) -> None:
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _audio_manifest_sha256(metadata: list[dict]) -> str:
    digest = hashlib.sha256()
    for chunk in metadata:
        digest.update(str(int(chunk["sequence"])).encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(chunk.get("chunk_id") or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(chunk.get("sha256") or "").encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(chunk.get("content_type") or "").encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fact_manifest_sha256(claims: list[dict]) -> str:
    canonical = [
        {
            "id": claim.get("id"),
            "status": claim.get("status"),
            "verdict": claim.get("verdict"),
            "sources": claim.get("sources") or [],
        }
        for claim in claims
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seconds_label(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):.3f}s"
    except (TypeError, ValueError):
        return None


def _audio_extension(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "audio/webm": ".webm",
        "video/webm": ".webm",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
    }.get(media_type, ".webm")


def render_minutes_markdown(data: dict) -> str:
    lines = [f"# {data.get('title') or '회의록'}", "", data.get("summary", "").strip(), ""]
    sections = (
        ("안건", "agenda", lambda item: f"{item.get('topic', '')} — {item.get('outcome', '')}"),
        ("결정 사항", "decisions", lambda item: item.get("text", "")),
        (
            "실행 항목",
            "action_items",
            lambda item: " · ".join(
                part
                for part in (
                    item.get("text", ""),
                    f"담당: {item.get('assignee') or '미정'}",
                    f"기한: {item.get('due_at') or '미정'}",
                )
                if part
            ),
        ),
        ("미결 사항", "open_questions", lambda item: item.get("text", "")),
        (
            "팩트 확인",
            "facts",
            lambda item: f"[{item.get('status', 'insufficient')}] {item.get('claim', '')} {item.get('verdict', '')}",
        ),
    )
    for title, key, formatter in sections:
        lines.extend([f"## {title}", ""])
        items = data.get(key) or []
        if not items:
            lines.append("- 없음")
        else:
            lines.extend(f"- {formatter(item).strip()}" for item in items)
        lines.append("")
    warnings = data.get("warnings") or []
    if warnings:
        lines.extend(["## 확인 필요", "", *[f"- {warning}" for warning in warnings], ""])
    return "\n".join(lines).strip() + "\n"
