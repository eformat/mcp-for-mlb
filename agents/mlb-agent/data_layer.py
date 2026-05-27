"""Minimal in-memory data layer for Chainlit chat history sidebar."""

from collections import defaultdict
from typing import Dict, List, Optional
import uuid

from chainlit.data import BaseDataLayer
from chainlit.types import (
    Feedback,
    PageInfo,
    Pagination,
    ThreadDict,
    ThreadFilter,
    PaginatedResponse,
)
from chainlit.element import Element
from chainlit.step import StepDict
from chainlit.user import PersistedUser, User


class InMemoryDataLayer(BaseDataLayer):
    """Simple in-memory data layer — threads survive within a pod lifecycle."""

    def __init__(self):
        self.users: Dict[str, PersistedUser] = {}
        self.threads: Dict[str, ThreadDict] = {}
        self.steps: Dict[str, StepDict] = {}
        self.elements: Dict[str, dict] = {}
        self.feedbacks: Dict[str, Feedback] = {}
        self.favorites: Dict[str, List[StepDict]] = defaultdict(list)

    def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        from datetime import datetime
        persisted = PersistedUser(
            id=user.identifier,
            identifier=user.identifier,
            metadata=user.metadata or {},
            createdAt=datetime.now().isoformat(),
        )
        self.users[user.identifier] = persisted
        return persisted

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        return self.users.get(identifier)

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        if thread_id not in self.threads:
            from datetime import datetime
            self.threads[thread_id] = {
                "id": thread_id,
                "name": name or thread_id[:20],
                "userId": user_id,
                "metadata": metadata or {},
                "tags": tags or [],
                "createdAt": datetime.now().isoformat(),
                "steps": [],
                "elements": [],
            }
        else:
            thread = self.threads[thread_id]
            if name:
                thread["name"] = name
            if user_id:
                thread["userId"] = user_id
            if metadata:
                thread["metadata"] = metadata
            if tags:
                thread["tags"] = tags

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        return self.threads.get(thread_id)

    async def get_thread_author(self, thread_id: str) -> str:
        thread = self.threads.get(thread_id)
        if thread:
            return thread.get("userId", "unknown")
        return "unknown"

    async def delete_thread(self, thread_id: str):
        self.threads.pop(thread_id, None)

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        threads = list(self.threads.values())

        if filters.userId:
            threads = [t for t in threads if t.get("userId") == filters.userId]

        threads = list(reversed(threads))

        return PaginatedResponse(
            data=threads,
            pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
        )

    async def create_step(self, step_dict: StepDict):
        step_id = step_dict.get("id", str(uuid.uuid4()))
        self.steps[step_id] = step_dict

        thread_id = step_dict.get("threadId")
        if thread_id and thread_id in self.threads:
            self.threads[thread_id].setdefault("steps", []).append(step_dict)

    async def update_step(self, step_dict: StepDict):
        step_id = step_dict.get("id")
        if step_id:
            self.steps[step_id] = step_dict

    async def delete_step(self, step_id: str):
        self.steps.pop(step_id, None)

    async def create_element(self, element: Element):
        self.elements[element.id] = element.to_dict()

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional[dict]:
        return self.elements.get(element_id)

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        self.elements.pop(element_id, None)

    async def upsert_feedback(self, feedback: Feedback) -> str:
        fid = feedback.id or str(uuid.uuid4())
        feedback.id = fid
        self.feedbacks[fid] = feedback
        return fid

    async def delete_feedback(self, feedback_id: str) -> bool:
        return self.feedbacks.pop(feedback_id, None) is not None

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        return self.favorites.get(user_id, [])

    async def set_step_favorite(self, step_id: str, user_id: str, favorite: bool):
        if favorite:
            step = self.steps.get(step_id)
            if step:
                self.favorites[user_id].append(step)
        else:
            self.favorites[user_id] = [
                s for s in self.favorites[user_id] if s.get("id") != step_id
            ]
