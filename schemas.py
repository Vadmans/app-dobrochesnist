from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class EventIn(BaseModel):
    cat: str
    title: str
    date: date
    recur: str = ""
    description: str = ""
    instruction: str = ""
    audience: str = "Усі працівники"
    link: str = ""
    reminders: List[int] = []


class EventOut(EventIn):
    id: str
    views: int
    class Config: from_attributes = True


class ReferenceIn(BaseModel):
    title: str
    description: str = ""
    link: str = ""


class ReferenceOut(ReferenceIn):
    id: str
    class Config: from_attributes = True


class DeviceIn(BaseModel):
    token: str
    platform: str = "android"
    app_version: str = ""
    client_id: str = ""


class PushIn(BaseModel):
    title: str
    body: str


class ChatQuestionIn(BaseModel):
    client_id: str
    question: str


class ChatAnswerIn(BaseModel):
    answer: str


class ChatMessageOut(BaseModel):
    id: str
    client_id: str
    question: str
    answer: str = ""
    status: str = "new"
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    class Config: from_attributes = True

