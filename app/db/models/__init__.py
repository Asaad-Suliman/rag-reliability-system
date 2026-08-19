"""ORM models.

Alembic imports `app.db.base` for metadata; every model module must be imported
here to register its table on `Base.metadata` and be picked up by autogenerate.
"""

from app.db.models.audit import AuditEvent
from app.db.models.chunk import Chunk
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.message import Message
from app.db.models.user import User

__all__ = ["AuditEvent", "Chunk", "Conversation", "Document", "Message", "User"]
